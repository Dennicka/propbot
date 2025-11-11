"""Lightweight audit snapshot utilities."""

from __future__ import annotations

from typing import Any, List, Mapping

from app.audit_log import list_recent_operator_actions


_DEFAULT_LIMIT = 100


def get_recent_audit_snapshot(limit: int = _DEFAULT_LIMIT) -> List[Mapping[str, Any]]:
    """Return a recent slice of operator audit entries for diagnostics."""

    return list_recent_operator_actions(limit=limit)


__all__ = ["get_recent_audit_snapshot"]
