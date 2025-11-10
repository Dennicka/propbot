"""Persistence helpers for runtime restart snapshots."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


LOGGER = logging.getLogger(__name__)

_SNAPSHOT_PATH_ENV = "RUNTIME_SNAPSHOT_PATH"
_DEFAULT_SNAPSHOT_PATH = Path("data/runtime_snapshot.json")
_SCHEMA_VERSION = 1


def _resolve_path() -> Path:
    override = os.environ.get(_SNAPSHOT_PATH_ENV)
    if override:
        return Path(override)
    return _DEFAULT_SNAPSHOT_PATH


def _normalise_mapping(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    return {str(key): value for key, value in payload.items()}


def _normalise_positions(entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalised: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        record = {str(key): value for key, value in entry.items()}
        status = str(record.get("status") or "").lower()
        if status == "closed":
            continue
        normalised.append(record)
    return normalised


def dump(
    *, control: Mapping[str, Any], safety: Mapping[str, Any], positions: Iterable[Mapping[str, Any]]
) -> Path | None:
    """Persist the provided runtime snapshot to disk."""

    path = _resolve_path()
    payload = {
        "version": _SCHEMA_VERSION,
        "snapshot_ts": datetime.now(timezone.utc).isoformat(),
        "control": _normalise_mapping(control),
        "safety": _normalise_mapping(safety),
        "positions": _normalise_positions(positions),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
    except OSError:
        LOGGER.exception("failed to write runtime snapshot", extra={"path": str(path)})
        return None
    return path


def load() -> dict[str, Any]:
    """Load the persisted runtime snapshot from disk."""

    path = _resolve_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.warning("runtime snapshot corrupted", extra={"path": str(path)})
        return {}
    if not isinstance(payload, Mapping):
        return {}
    version = payload.get("version")
    try:
        version_num = int(version)
    except (TypeError, ValueError):
        version_num = 0
    if version_num != _SCHEMA_VERSION:
        LOGGER.info(
            "runtime snapshot version mismatch", extra={"path": str(path), "version": version}
        )
        return {}
    control = _normalise_mapping(payload.get("control"))
    safety = _normalise_mapping(payload.get("safety"))
    positions_payload = payload.get("positions")
    if isinstance(positions_payload, Iterable):
        positions = _normalise_positions(
            entry for entry in positions_payload if isinstance(entry, Mapping)
        )
    else:
        positions = []
    return {"control": control, "safety": safety, "positions": positions}


__all__ = ["dump", "load"]
