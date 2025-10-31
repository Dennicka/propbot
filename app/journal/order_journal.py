"""Exactly-once journal backed by the shared ledger SQLite database."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Tuple

from .. import ledger
from . import is_enabled

LOGGER = logging.getLogger(__name__)

# Reuse the ledger lock to guarantee cross-table consistency.
_LEDGER_LOCK: threading.Lock = getattr(ledger, "_LEDGER_LOCK")  # type: ignore[attr-defined]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS order_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            ts TEXT NOT NULL,
            type TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """
    )


def _fetch_row(cursor: sqlite3.Cursor | sqlite3.Connection, *, uuid: str | None = None, row_id: int | None = None) -> Dict[str, Any] | None:
    if uuid is not None:
        row = cursor.execute(
            "SELECT id, uuid, ts, type, payload FROM order_journal WHERE uuid = ?",
            (uuid,),
        ).fetchone()
    elif row_id is not None:
        row = cursor.execute(
            "SELECT id, uuid, ts, type, payload FROM order_journal WHERE id = ?",
            (int(row_id),),
        ).fetchone()
    else:  # pragma: no cover - defensive
        return None
    if row is None:
        return None
    payload = row["payload"] if isinstance(row, sqlite3.Row) else row[4]
    if isinstance(payload, (bytes, bytearray)):
        payload_text = payload.decode("utf-8")
    else:
        payload_text = str(payload)
    try:
        payload_obj = json.loads(payload_text)
    except json.JSONDecodeError:
        payload_obj = payload_text
    return {
        "id": int(row["id"] if isinstance(row, sqlite3.Row) else row[0]),
        "uuid": str(row["uuid"] if isinstance(row, sqlite3.Row) else row[1]),
        "ts": str(row["ts"] if isinstance(row, sqlite3.Row) else row[2]),
        "type": str(row["type"] if isinstance(row, sqlite3.Row) else row[3]),
        "payload": payload_obj,
    }


def append(event: Mapping[str, Any]) -> Dict[str, Any]:
    """Insert *event* into the journal and return the stored record.

    The event must provide ``uuid``, ``ts``, ``type`` and ``payload`` keys. Duplicate
    UUIDs reuse the stored record and return it with ``created=False``.
    """

    if not is_enabled():
        return {
            "uuid": str(event.get("uuid")),
            "ts": str(event.get("ts")),
            "type": str(event.get("type")),
            "payload": event.get("payload"),
            "created": False,
        }

    uuid_value = str(event.get("uuid") or uuid.uuid4())
    ts_value = str(event.get("ts") or _now())
    type_value = str(event.get("type") or "unknown")
    payload_value = event.get("payload", {})
    payload_text = json.dumps(payload_value, separators=(",", ":"))

    with _LEDGER_LOCK:
        conn = ledger._connect()  # type: ignore[attr-defined]
        with conn:
            _ensure_table(conn)
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO order_journal (uuid, ts, type, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (uuid_value, ts_value, type_value, payload_text),
                )
            except sqlite3.IntegrityError:
                record = _fetch_row(conn, uuid=uuid_value)
                if record is None:
                    raise
                record["created"] = False
                return record
            row_id = int(cursor.lastrowid)
            record = _fetch_row(conn, row_id=row_id)
            if record is None:  # pragma: no cover - defensive
                record = {
                    "id": row_id,
                    "uuid": uuid_value,
                    "ts": ts_value,
                    "type": type_value,
                    "payload": payload_value,
                }
            record["created"] = True
            return record


def get(uuid_value: str) -> Dict[str, Any] | None:
    """Return the journal entry for *uuid_value* if present."""
    if not is_enabled():
        return None
    with _LEDGER_LOCK:
        conn = ledger._connect()  # type: ignore[attr-defined]
        record = _fetch_row(conn, uuid=uuid_value)
        return record


def get_since(*, ts: str | None = None, uuid_value: str | None = None, limit: int = 500) -> List[Dict[str, Any]]:
    """Return journal entries strictly after ``ts`` or ``uuid_value``.

    When ``uuid_value`` is provided the timestamp of that entry is resolved first.
    Results are ordered by ``ts`` ascending.
    """
    if not is_enabled():
        return []
    with _LEDGER_LOCK:
        conn = ledger._connect()  # type: ignore[attr-defined]
        with conn:
            _ensure_table(conn)
            ts_cutoff = ts
            if uuid_value:
                row = conn.execute(
                    "SELECT ts FROM order_journal WHERE uuid = ?",
                    (uuid_value,),
                ).fetchone()
                if row:
                    ts_cutoff = str(row["ts"] if isinstance(row, sqlite3.Row) else row[0])
            params: Tuple[Any, ...]
            if ts_cutoff:
                rows = conn.execute(
                    """
                    SELECT id, uuid, ts, type, payload
                    FROM order_journal
                    WHERE ts > ?
                    ORDER BY ts ASC
                    LIMIT ?
                    """,
                    (ts_cutoff, int(limit)),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, uuid, ts, type, payload
                    FROM order_journal
                    ORDER BY ts ASC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
    entries: List[Dict[str, Any]] = []
    for row in rows:
        payload_text = row["payload"] if isinstance(row, sqlite3.Row) else row[4]
        if isinstance(payload_text, (bytes, bytearray)):
            payload_str = payload_text.decode("utf-8")
        else:
            payload_str = str(payload_text)
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            payload = payload_str
        entries.append(
            {
                "id": int(row["id"] if isinstance(row, sqlite3.Row) else row[0]),
                "uuid": str(row["uuid"] if isinstance(row, sqlite3.Row) else row[1]),
                "ts": str(row["ts"] if isinstance(row, sqlite3.Row) else row[2]),
                "type": str(row["type"] if isinstance(row, sqlite3.Row) else row[3]),
                "payload": payload,
            }
        )
    return entries


def healthcheck() -> bool:
    """Best-effort read/write probe for the journal."""
    if not is_enabled():
        return True
    try:
        record = append(
            {
                "uuid": "journal-healthcheck",
                "ts": _now(),
                "type": "healthcheck",
                "payload": {"status": "ok"},
            }
        )
    except Exception:  # pragma: no cover - defensive logging
        LOGGER.exception("journal healthcheck failed")
        return False
    return bool(record)


__all__ = ["append", "get", "get_since", "healthcheck"]
