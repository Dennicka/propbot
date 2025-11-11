"""Persistent hedge execution journal for auto mode."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, List, Mapping

from ..runtime import leader_lock


LOGGER = logging.getLogger(__name__)


def _log_path() -> Path:
    override = os.getenv("HEDGE_LOG_PATH")
    if override:
        return Path(override)
    return Path("data/hedge_log.json")


def _load_entries(path: Path) -> List[dict]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def read_entries(*, limit: int | None = None) -> List[dict]:
    """Return stored hedge log entries (most recent last)."""

    entries = _load_entries(_log_path())
    if limit is not None and limit >= 0:
        return entries[-limit:]
    return entries


def append_entry(entry: Mapping[str, Any]) -> dict:
    """Append *entry* to the persistent hedge log and return it as a dict."""

    record = dict(entry)
    record = leader_lock.attach_fencing_meta(record)
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.warning("hedge_log parent creation failed path=%s error=%s", path.parent, exc)

    entries = _load_entries(path)
    entries.append(record)

    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(entries, handle, indent=2, sort_keys=True)
    except OSError as exc:
        # Best-effort persistence; ignore failures but still return the record.
        LOGGER.warning("hedge_log write failed path=%s error=%s", path, exc)
    return record


def reset_log() -> None:
    """Clear the hedge log (used in tests)."""

    path = _log_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        # If unlink fails, overwrite with empty list.
        LOGGER.warning("hedge_log unlink failed path=%s error=%s", path, exc)
        try:
            with path.open("w", encoding="utf-8") as handle:
                json.dump([], handle)
        except OSError as rewrite_exc:
            LOGGER.error("hedge_log reset failed path=%s error=%s", path, rewrite_exc)


__all__ = ["append_entry", "read_entries", "reset_log"]
