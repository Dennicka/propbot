"""Audit logging helpers."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union


_AUDIT_LOG_PATH = Path("data/audit.log")


def _serialize_details(
    details: Optional[Union[Dict[str, Any], Mapping[str, Any], str, Any]]
) -> Optional[Union[Dict[str, Any], str]]:
    if details is None:
        return None
    if isinstance(details, str):
        return details
    if not isinstance(details, Mapping):
        return str(details)
    sanitized: Dict[str, Any] = {}
    for key, value in details.items():
        if key and "key" in key.lower():
            sanitized[key] = "***"
        elif key and "secret" in key.lower():
            sanitized[key] = "***"
        else:
            sanitized[key] = value
    return sanitized


def log_operator_action(
    operator_name: str,
    role: str,
    action: str,
    channel: str,
    details: Optional[Union[Dict[str, Any], Mapping[str, Any], str, Any]] = None,
) -> None:
    """Write an operator action to the audit log."""

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operator": operator_name,
        "role": role,
        "action": action,
        "channel": channel,
        "details": _serialize_details(details),
    }

    _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


__all__ = ["log_operator_action"]
