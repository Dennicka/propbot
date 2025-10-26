"""Persistent hedge execution journal for auto mode."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List, Mapping


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
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    entries = _load_entries(path)
    entries.append(record)

    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(entries, handle, indent=2, sort_keys=True)
    except OSError:
        # Best-effort persistence; ignore failures but still return the record.
        pass
    return record


def reset_log() -> None:
    """Clear the hedge log (used in tests)."""

    path = _log_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        # If unlink fails, overwrite with empty list.
        try:
            with path.open("w", encoding="utf-8") as handle:
                json.dump([], handle)
        except OSError:
            pass


__all__ = ["append_entry", "read_entries", "reset_log"]
