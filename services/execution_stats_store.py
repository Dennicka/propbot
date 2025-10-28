"""Persistent store for execution quality statistics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

_DEFAULT_PATH = Path("data/execution_stats.json")
_MAX_RECORDS = 500


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _load_entries(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(payload, list):
        result = []
        for entry in payload:
            if isinstance(entry, dict):
                result.append(entry)
        return result
    return []


def append_entry(entry: Dict[str, Any], *, path: Path = _DEFAULT_PATH) -> None:
    record = dict(entry)
    _ensure_parent(path)
    entries = _load_entries(path)
    entries.append(record)
    if len(entries) > _MAX_RECORDS:
        entries = entries[-_MAX_RECORDS:]
    path.write_text(json.dumps(entries, indent=2, sort_keys=True))


def list_recent(limit: int = 20, *, path: Path = _DEFAULT_PATH) -> List[Dict[str, Any]]:
    entries = _load_entries(path)
    if limit <= 0:
        return []
    return list(entries[-limit:])


def extend(entries: Iterable[Dict[str, Any]], *, path: Path = _DEFAULT_PATH) -> None:
    existing = _load_entries(path)
    existing.extend(dict(entry) for entry in entries if isinstance(entry, dict))
    if len(existing) > _MAX_RECORDS:
        existing = existing[-_MAX_RECORDS:]
    _ensure_parent(path)
    path.write_text(json.dumps(existing, indent=2, sort_keys=True))


__all__ = ["append_entry", "list_recent", "extend"]
