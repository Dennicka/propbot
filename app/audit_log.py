"""Audit logging helpers with in-memory snapshot support."""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Mapping, MutableMapping, Optional, Sequence, Union


_AUDIT_LOG_PATH = Path(os.getenv("AUDIT_LOG_PATH") or "data/audit.log")
_IN_MEMORY_LIMIT = 500
_IN_MEMORY_LOG: deque[dict[str, Any]] = deque(maxlen=_IN_MEMORY_LIMIT)
_LOCK = Lock()


LOGGER = logging.getLogger(__name__)


def _sanitize_mapping(details: Mapping[str, Any]) -> Dict[str, Any]:
    sanitized: Dict[str, Any] = {}
    for key, value in details.items():
        key_str = str(key) if key is not None else ""
        lowered = key_str.lower()
        if any(token in lowered for token in ("key", "secret", "token")):
            sanitized[key_str] = "***"
            continue
        if isinstance(value, Mapping):
            sanitized[key_str] = _sanitize_mapping(value)
        elif isinstance(value, list):
            sanitized[key_str] = [
                _sanitize_mapping(item) if isinstance(item, Mapping) else item
                for item in value
            ]
        else:
            sanitized[key_str] = value
    return sanitized


def _serialize_details(
    details: Optional[Union[Dict[str, Any], Mapping[str, Any], str, Any]]
) -> Optional[Union[Dict[str, Any], str]]:
    if details is None:
        return None
    if isinstance(details, str):
        return details
    if not isinstance(details, Mapping):
        return str(details)
    return _sanitize_mapping(details)


def _coerce_timestamp(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(timezone.utc).isoformat()


def _normalise_entry(entry: MutableMapping[str, Any]) -> dict[str, Any]:
    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    entry["timestamp"] = _coerce_timestamp(entry.get("timestamp"))
    if "operator_name" in entry:
        entry.setdefault("operator", entry.get("operator_name"))
    operator_name = entry.get("operator") or entry.get("operator_name") or "unknown"
    role = entry.get("role") or "unknown"
    action = entry.get("action") or "UNKNOWN"
    details = _serialize_details(entry.get("details"))
    return {
        "timestamp": entry["timestamp"],
        "operator_name": str(operator_name),
        "role": str(role),
        "action": str(action),
        "details": details,
    }


def _load_in_memory_log(path: Path) -> None:
    if not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        LOGGER.error(
            "failed to read audit log snapshot; continuing with empty buffer",
            extra={"path": str(path)},
            exc_info=True,
        )
        return
    if not raw.strip():
        return
    lines = raw.splitlines()
    for line in lines[-_IN_MEMORY_LIMIT:]:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            LOGGER.warning(
                "invalid audit log entry skipped",
                extra={"path": str(path)},
                exc_info=True,
            )
            continue
        if not isinstance(payload, MutableMapping):
            continue
        normalised = _normalise_entry(payload)
        _IN_MEMORY_LOG.append(normalised)


_load_in_memory_log(_AUDIT_LOG_PATH)


def log_operator_action(
    operator_name: str,
    role: str,
    action: str,
    details: Optional[Union[Dict[str, Any], Mapping[str, Any], str, Any]] = None,
) -> None:
    """Write an operator action to the audit log and in-memory snapshot."""

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operator_name": operator_name,
        "operator": operator_name,
        "role": role,
        "action": action,
        "details": _serialize_details(details),
    }

    with _LOCK:
        _IN_MEMORY_LOG.append(dict(entry))
        try:
            _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            LOGGER.error(
                "failed to ensure audit log directory",
                extra={"path": str(_AUDIT_LOG_PATH)},
                exc_info=True,
            )
            return
        try:
            with _AUDIT_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            LOGGER.error(
                "failed to append to audit log",
                extra={"path": str(_AUDIT_LOG_PATH)},
                exc_info=True,
            )


def list_recent_operator_actions(limit: int = 100) -> list[dict[str, Any]]:
    """Return the most recent operator actions from the in-memory buffer."""

    if limit <= 0:
        return []
    limit = min(limit, _IN_MEMORY_LIMIT)
    with _LOCK:
        entries: Sequence[dict[str, Any]] = list(_IN_MEMORY_LOG)
    if not entries:
        return []
    return [dict(item) for item in entries[-limit:]]


__all__ = ["log_operator_action", "list_recent_operator_actions"]
