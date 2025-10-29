"""Role-based access control utilities."""
from __future__ import annotations

from typing import Literal

Role = Literal["viewer", "auditor", "operator"]
Action = Literal["HOLD", "RESUME", "KILL", "RAISE_LIMITS", "CANCEL_ALL"]


_CRITICAL_ACTIONS = {"HOLD", "RESUME", "KILL", "RAISE_LIMITS", "CANCEL_ALL"}


def can_execute_action(role: Role, action: Action) -> bool:
    """Return ``True`` if ``role`` can perform ``action``.

    ``viewer`` roles are read-only and cannot perform critical actions, while
    ``operator`` roles are allowed to execute all supported actions.
    """

    if role == "operator":
        return True
    if role in {"viewer", "auditor"}:
        return action not in _CRITICAL_ACTIONS
    return False


__all__ = ["can_execute_action", "Role", "Action"]
