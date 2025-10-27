"""Disk-backed runtime status snapshot helper."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


_DEFAULT_RUNTIME_PATH = Path("data/runtime_state.json")


def get_runtime_state_path() -> Path:
    """Resolve the configured runtime state path."""

    override = os.environ.get("RUNTIME_STATE_PATH")
    if override:
        return Path(override)
    return _DEFAULT_RUNTIME_PATH


def load_runtime_payload() -> dict[str, Any]:
    """Load the persisted runtime payload from disk."""

    path = get_runtime_state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    return dict(payload)


def write_runtime_payload(payload: Mapping[str, Any]) -> None:
    """Persist the provided payload to disk with pretty formatting."""

    path = get_runtime_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    serialisable = dict(payload)
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(serialisable, handle, indent=2, sort_keys=True)
    except OSError:
        pass


def merge_runtime_payload(updates: Mapping[str, Any]) -> dict[str, Any]:
    """Update the existing payload with ``updates`` and persist it."""

    payload = load_runtime_payload()
    payload.update(updates)
    write_runtime_payload(payload)
    return payload
