"""Lightweight leader election using a sqlite-backed fencing lock."""

from __future__ import annotations

import errno
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping


LOGGER = logging.getLogger(__name__)


@dataclass
class _LeaderState:
    owner: str | None = None
    expires_at: float = 0.0
    acquired: bool = False
    last_error: str | None = None
    fencing_id: str | None = None
    heartbeat_ts: float = 0.0


@dataclass(frozen=True)
class Heartbeat:
    """Serialized leader heartbeat information."""

    pid: int | None = None
    fencing_id: str | None = None
    timestamp: float | None = None

    def age(self, *, now: float | None = None) -> float | None:
        if self.timestamp is None:
            return None
        moment = now or time.time()
        return max(moment - self.timestamp, 0.0)


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


def _heartbeat_path() -> Path:
    override = os.getenv("LEADER_HEARTBEAT_PATH")
    if override:
        return Path(override)
    return Path("data/leader.hb")


def _ttl_seconds() -> int:
    ttl = max(_env_int("LEADER_LOCK_TTL_SEC", 30), 5)
    return ttl


def _stale_seconds() -> int:
    ttl = _ttl_seconds()
    default = max(int(ttl * 2), 60)
    return max(_env_int("LEADER_LOCK_STALE_SEC", default), ttl)


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
    except sqlite3.DatabaseError as exc:
        LOGGER.warning(
            "leader_lock.pragma_failed",
            extra={"path": str(path)},
            exc_info=exc,
        )
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
            fencing_id TEXT,
            updated_at REAL NOT NULL
        )
        """
    )
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info('leader_lock')").fetchall()
    }
    if "fencing_id" not in columns:
        conn.execute("ALTER TABLE leader_lock ADD COLUMN fencing_id TEXT")


def _record_local_state(
    *,
    owner: str | None,
    expires_at: float,
    acquired: bool,
    error: str | None = None,
    fencing_id: str | None = None,
    heartbeat_ts: float | None = None,
) -> None:
    with _STATE_LOCK:
        _STATE.owner = owner
        _STATE.expires_at = expires_at
        _STATE.acquired = acquired
        _STATE.last_error = error
        _STATE.fencing_id = fencing_id
        _STATE.heartbeat_ts = heartbeat_ts or 0.0


def _reset_db_on_error(exc: Exception) -> None:
    path = _db_path()
    try:
        path.unlink()
    except OSError as unlink_exc:
        if unlink_exc.errno != errno.ENOENT:
            LOGGER.error(
                "leader_lock.reset_unlink_failed",
                extra={"path": str(path)},
                exc_info=unlink_exc,
            )
    _record_local_state(owner=None, expires_at=0.0, acquired=False, error=str(exc), fencing_id=None)
    LOGGER.error(
        "leader_lock.reset_due_to_error",
        extra={"path": str(path)},
        exc_info=exc,
    )


def _write_heartbeat(data: Heartbeat) -> None:
    if data.fencing_id is None or data.timestamp is None:
        return
    path = _heartbeat_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.error(
            "leader_lock.heartbeat_parent_failed",
            extra={"path": str(path.parent)},
            exc_info=exc,
        )
        return
    payload = {
        "pid": data.pid,
        "fencing_id": data.fencing_id,
        "ts": data.timestamp,
    }
    try:
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    except OSError as exc:
        LOGGER.error(
            "leader_lock.heartbeat_write_failed",
            extra={"path": str(path)},
            exc_info=exc,
        )


def beat(*, fencing_id: str | None = None, now: float | None = None) -> Heartbeat:
    """Persist a leader heartbeat marker for fencing consumers."""

    if not feature_enabled():
        return Heartbeat()
    moment = now or time.time()
    pid = os.getpid()
    with _STATE_LOCK:
        if fencing_id is None:
            fencing_id = _STATE.fencing_id
        if not fencing_id:
            _STATE.heartbeat_ts = 0.0
            return Heartbeat()
        _STATE.heartbeat_ts = moment
    heartbeat = Heartbeat(pid=pid, fencing_id=fencing_id, timestamp=moment)
    _write_heartbeat(heartbeat)
    return heartbeat


def last_heartbeat() -> Heartbeat:
    """Return the last recorded heartbeat from disk."""

    path = _heartbeat_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            LOGGER.warning(
                "leader_lock.heartbeat_read_failed",
                extra={"path": str(path)},
                exc_info=exc,
            )
        return Heartbeat()
    if not raw.strip():
        return Heartbeat()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        LOGGER.error(
            "leader_lock.heartbeat_invalid_json",
            extra={"path": str(path)},
            exc_info=exc,
        )
        return Heartbeat()
    if not isinstance(payload, dict):
        return Heartbeat()
    pid_value = payload.get("pid")
    fencing_value = payload.get("fencing_id")
    ts_value = payload.get("ts")
    try:
        pid_int = int(pid_value) if pid_value is not None else None
    except (TypeError, ValueError):
        pid_int = None
    try:
        ts_float = float(ts_value) if ts_value is not None else None
    except (TypeError, ValueError):
        ts_float = None
    fencing_text = str(fencing_value).strip() if fencing_value is not None else ""
    return Heartbeat(
        pid=pid_int,
        fencing_id=fencing_text or None,
        timestamp=ts_float,
    )


def _current_meta() -> dict[str, object] | None:
    if not feature_enabled():
        return None
    with _STATE_LOCK:
        if not _STATE.acquired or not _STATE.fencing_id:
            return None
        return {"fencing_id": _STATE.fencing_id}


def attach_fencing_meta(payload: Mapping[str, object]) -> dict[str, object]:
    """Embed the current fencing id into a payload under ``meta`` if available."""

    if not isinstance(payload, dict):
        payload = dict(payload)
    else:
        payload = dict(payload)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else None
    fencing_meta = _current_meta()
    if not fencing_meta:
        return payload
    merged_meta = dict(meta or {})
    merged_meta.setdefault("fencing_id", fencing_meta["fencing_id"])
    payload["meta"] = merged_meta
    return payload


def acquire(*, now: float | None = None) -> bool:
    """Attempt to acquire or renew the leader lock.

    Returns ``True`` when the current process is the leader.
    """

    owner = _resolve_instance_id()
    moment = now or time.time()
    ttl = _ttl_seconds()
    stale = _stale_seconds()
    fencing_id: str | None = None
    try:
        with _connect() as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT owner, expires_at, fencing_id FROM leader_lock WHERE id = 1"
            ).fetchone()
            if row:
                row_owner = str(row["owner"] or "")
                row_expiry = float(row["expires_at"] or 0.0)
                row_fencing = str(row["fencing_id"] or "") or None
                if row_owner and row_owner != owner and row_expiry > moment:
                    heartbeat = last_heartbeat()
                    hb_age = heartbeat.age(now=moment)
                    hb_fencing = heartbeat.fencing_id
                    stale_allowed = False
                    if hb_age is not None and hb_age >= stale:
                        stale_allowed = True
                    if row_fencing and hb_fencing and hb_fencing != row_fencing:
                        stale_allowed = True
                    if not stale_allowed:
                        conn.rollback()
                        _record_local_state(
                            owner=row_owner,
                            expires_at=row_expiry,
                            acquired=False,
                            fencing_id=row_fencing,
                            heartbeat_ts=heartbeat.timestamp,
                        )
                        return False
                if row_owner == owner and row_expiry > moment and row_fencing:
                    fencing_id = row_fencing
            expiry = moment + ttl
            if not fencing_id:
                fencing_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO leader_lock (id, owner, expires_at, fencing_id, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    owner=excluded.owner,
                    expires_at=excluded.expires_at,
                    fencing_id=excluded.fencing_id,
                    updated_at=excluded.updated_at
                """,
                (owner, expiry, fencing_id, moment),
            )
            conn.commit()
    except sqlite3.DatabaseError as exc:
        _reset_db_on_error(exc)
        return False
    _record_local_state(
        owner=owner,
        expires_at=expiry,
        acquired=True,
        error=None,
        fencing_id=fencing_id,
        heartbeat_ts=moment,
    )
    beat(fencing_id=fencing_id, now=moment)
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
                _record_local_state(owner=None, expires_at=0.0, acquired=False, fencing_id=None)
                return False
            row_owner = str(row["owner"] or "")
            if row_owner != owner:
                conn.rollback()
                _record_local_state(owner=row_owner, expires_at=0.0, acquired=False, fencing_id=None)
                return False
            conn.execute("DELETE FROM leader_lock WHERE id = 1")
            conn.commit()
    except sqlite3.DatabaseError as exc:
        _reset_db_on_error(exc)
        return False
    _record_local_state(owner=None, expires_at=0.0, acquired=False, error=None, fencing_id=None)
    heartbeat_path = _heartbeat_path()
    try:
        heartbeat_path.unlink()
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            LOGGER.warning(
                "leader_lock.heartbeat_unlink_failed",
                extra={"path": str(heartbeat_path)},
                exc_info=exc,
            )
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
    heartbeat = last_heartbeat()
    heartbeat_age = heartbeat.age(now=moment)
    with _STATE_LOCK:
        remaining = max(_STATE.expires_at - moment, 0.0)
        return {
            "owner": _STATE.owner,
            "expires_at": _STATE.expires_at,
            "remaining": remaining,
            "leader": _STATE.acquired and remaining > 0,
            "last_error": _STATE.last_error,
            "fencing_id": _STATE.fencing_id,
            "heartbeat_ts": heartbeat.timestamp,
            "heartbeat_pid": heartbeat.pid,
            "heartbeat_age": heartbeat_age,
        }


def reset_for_tests() -> None:
    _record_local_state(owner=None, expires_at=0.0, acquired=False, error=None, fencing_id=None)
    global _INSTANCE_ID
    _INSTANCE_ID = None
    path = _db_path()
    try:
        path.unlink()
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            LOGGER.warning(
                "leader_lock.reset_db_unlink_failed",
                extra={"path": str(path)},
                exc_info=exc,
            )
    hb_path = _heartbeat_path()
    try:
        hb_path.unlink()
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            LOGGER.warning(
                "leader_lock.reset_heartbeat_unlink_failed",
                extra={"path": str(hb_path)},
                exc_info=exc,
            )


__all__ = [
    "acquire",
    "attach_fencing_meta",
    "beat",
    "feature_enabled",
    "get_status",
    "is_leader",
    "last_heartbeat",
    "release",
    "renew_interval",
    "reset_for_tests",
]
