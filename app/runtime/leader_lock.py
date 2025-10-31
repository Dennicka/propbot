"""Lightweight leader election using a sqlite-backed fencing lock."""

from __future__ import annotations

import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class _LeaderState:
    owner: str | None = None
    expires_at: float = 0.0
    acquired: bool = False
    last_error: str | None = None


_STATE = _LeaderState()
_STATE_LOCK = threading.Lock()
_INSTANCE_ID: str | None = None


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def feature_enabled() -> bool:
    """Return ``True`` when leader locking is enabled via env flag."""

    return _env_flag("FEATURE_LEADER_LOCK", False)


def _db_path() -> Path:
    override = os.getenv("LEADER_LOCK_PATH")
    if override:
        return Path(override)
    return Path("data/leader.lock")


def _ttl_seconds() -> int:
    ttl = max(_env_int("LEADER_LOCK_TTL_SEC", 30), 5)
    return ttl


def renew_interval() -> float:
    """Return the recommended renew interval for background schedulers."""

    ttl = _ttl_seconds()
    return max(2.0, ttl * 0.4)


def _resolve_instance_id() -> str:
    override = os.getenv("LEADER_LOCK_INSTANCE_ID")
    if override:
        return override.strip()
    global _INSTANCE_ID
    if _INSTANCE_ID is not None:
        return _INSTANCE_ID
    host = socket.gethostname()
    token = uuid.uuid4().hex[:8]
    _INSTANCE_ID = f"{host}:{os.getpid()}:{token}"
    return _INSTANCE_ID


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    try:
        yield conn
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leader_lock (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            owner TEXT NOT NULL,
            expires_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )


def _record_local_state(*, owner: str | None, expires_at: float, acquired: bool, error: str | None = None) -> None:
    with _STATE_LOCK:
        _STATE.owner = owner
        _STATE.expires_at = expires_at
        _STATE.acquired = acquired
        _STATE.last_error = error


def _reset_db_on_error(exc: Exception) -> None:
    path = _db_path()
    try:
        path.unlink()
    except OSError:
        pass
    _record_local_state(owner=None, expires_at=0.0, acquired=False, error=str(exc))


def acquire(*, now: float | None = None) -> bool:
    """Attempt to acquire or renew the leader lock.

    Returns ``True`` when the current process is the leader.
    """

    owner = _resolve_instance_id()
    moment = now or time.time()
    ttl = _ttl_seconds()
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner, expires_at FROM leader_lock WHERE id = 1").fetchone()
            if row:
                row_owner = str(row["owner"] or "")
                row_expiry = float(row["expires_at"] or 0.0)
                if row_owner and row_owner != owner and row_expiry > moment:
                    conn.rollback()
                    _record_local_state(owner=row_owner, expires_at=row_expiry, acquired=False)
                    return False
            expiry = moment + ttl
            conn.execute(
                """
                INSERT INTO leader_lock (id, owner, expires_at, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    owner=excluded.owner,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (owner, expiry, moment),
            )
            conn.commit()
    except sqlite3.DatabaseError as exc:
        _reset_db_on_error(exc)
        return False
    _record_local_state(owner=owner, expires_at=expiry, acquired=True, error=None)
    return True


def release() -> bool:
    """Release the leader lock if currently held by this instance."""

    owner = _resolve_instance_id()
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner FROM leader_lock WHERE id = 1").fetchone()
            if not row:
                conn.rollback()
                _record_local_state(owner=None, expires_at=0.0, acquired=False)
                return False
            row_owner = str(row["owner"] or "")
            if row_owner != owner:
                conn.rollback()
                _record_local_state(owner=row_owner, expires_at=0.0, acquired=False)
                return False
            conn.execute("DELETE FROM leader_lock WHERE id = 1")
            conn.commit()
    except sqlite3.DatabaseError as exc:
        _reset_db_on_error(exc)
        return False
    _record_local_state(owner=None, expires_at=0.0, acquired=False, error=None)
    return True


def is_leader(*, now: float | None = None) -> bool:
    """Return ``True`` if this process currently holds a valid lock."""

    moment = now or time.time()
    with _STATE_LOCK:
        if not _STATE.acquired:
            return False
        if moment >= _STATE.expires_at:
            _STATE.acquired = False
            return False
        return True


def get_status(*, now: float | None = None) -> dict[str, object]:
    moment = now or time.time()
    with _STATE_LOCK:
        remaining = max(_STATE.expires_at - moment, 0.0)
        return {
            "owner": _STATE.owner,
            "expires_at": _STATE.expires_at,
            "remaining": remaining,
            "leader": _STATE.acquired and remaining > 0,
            "last_error": _STATE.last_error,
        }


def reset_for_tests() -> None:
    _record_local_state(owner=None, expires_at=0.0, acquired=False, error=None)
    global _INSTANCE_ID
    _INSTANCE_ID = None
    path = _db_path()
    try:
        path.unlink()
    except OSError:
        pass


__all__ = [
    "acquire",
    "feature_enabled",
    "get_status",
    "is_leader",
    "release",
    "renew_interval",
    "reset_for_tests",
]
