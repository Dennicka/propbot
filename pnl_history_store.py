"""Persistence layer for rolling PnL and exposure snapshots."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Iterable, List, Mapping


_STORE_ENV = "PNL_HISTORY_PATH"
_DEFAULT_PATH = Path("data/pnl_history.json")
_LOCK = threading.RLock()


LOGGER = logging.getLogger(__name__)


def _store_path() -> Path:
    override = os.environ.get(_STORE_ENV)
    if override:
        return Path(override)
    return _DEFAULT_PATH


def get_store_path() -> Path:
    """Return the resolved path for the PnL history store."""

    return _store_path()


def _ensure_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.error(
            "pnl_history.parent_mkdir_failed",
            extra={"path": str(path.parent)},
            exc_info=exc,
        )


def _load_entries(path: Path) -> List[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        LOGGER.warning(
            "pnl_history.read_failed",
            extra={"path": str(path)},
            exc_info=exc,
        )
        return []
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        LOGGER.error(
            "pnl_history.invalid_json",
            extra={"path": str(path)},
            exc_info=exc,
        )
        return []
    if not isinstance(payload, list):
        return []
    entries: List[dict[str, Any]] = []
    for row in payload:
        if isinstance(row, Mapping):
            entries.append({str(key): value for key, value in row.items()})
    return entries


def _write_entries(path: Path, entries: Iterable[Mapping[str, Any]]) -> None:
    snapshot = [dict(row) for row in entries]
    _ensure_parent(path)
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, sort_keys=True)
    except OSError as exc:
        LOGGER.error(
            "pnl_history.write_failed",
            extra={"path": str(path)},
            exc_info=exc,
        )


def append_snapshot(
    snapshot: Mapping[str, Any], *, max_entries: int | None = 288
) -> dict[str, Any]:
    """Append ``snapshot`` to the on-disk history, enforcing ``max_entries``."""

    payload = dict(snapshot)
    path = _store_path()
    with _LOCK:
        entries = _load_entries(path)
        entries.append(payload)
        if max_entries is not None and max_entries > 0:
            entries = entries[-max_entries:]
        _write_entries(path, entries)
    return payload


def list_snapshots() -> List[dict[str, Any]]:
    """Return all persisted snapshots ordered from oldest to newest."""

    path = _store_path()
    with _LOCK:
        entries = _load_entries(path)
    return [dict(entry) for entry in entries]


def list_recent(limit: int | None = None) -> List[dict[str, Any]]:
    """Return the ``limit`` most recent snapshots (newest first)."""

    entries = list_snapshots()
    if limit is not None and limit > 0:
        entries = entries[-limit:]
    entries.reverse()
    return entries


def reset_store() -> None:
    """Clear the history store (used in tests)."""

    path = _store_path()
    with _LOCK:
        _write_entries(path, [])


__all__ = [
    "append_snapshot",
    "get_store_path",
    "list_recent",
    "list_snapshots",
    "reset_store",
]
