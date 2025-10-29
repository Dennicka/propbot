"""Role-based access control utilities."""
from __future__ import annotations

from typing import FrozenSet, Literal

Role = Literal["viewer", "auditor", "operator"]
Action = Literal[
    "HOLD",
    "RESUME_REQUEST",
    "RESUME_APPROVE",
    "RESUME_EXECUTE",
    "KILL_REQUEST",
    "KILL_APPROVE",
    "UNFREEZE_STRATEGY_REQUEST",
    "UNFREEZE_STRATEGY_APPROVE",
    "CANCEL_ALL",
    "SET_STRATEGY_ENABLED",
]


_ROLE_PERMISSIONS: dict[Role, FrozenSet[Action]] = {
    "viewer": frozenset(),
    "auditor": frozenset(),
    "operator": frozenset(
        {
            "HOLD",
            "RESUME_REQUEST",
            "RESUME_APPROVE",
            "RESUME_EXECUTE",
            "KILL_REQUEST",
            "KILL_APPROVE",
            "UNFREEZE_STRATEGY_REQUEST",
            "UNFREEZE_STRATEGY_APPROVE",
            "CANCEL_ALL",
            "SET_STRATEGY_ENABLED",
        }
    ),
}


def can_execute_action(role: Role, action: Action) -> bool:
    """Return ``True`` if ``role`` can perform ``action``."""

    allowed = _ROLE_PERMISSIONS.get(role)
    if allowed is None:
        return False
    return action in allowed


__all__ = ["can_execute_action", "Role", "Action"]
