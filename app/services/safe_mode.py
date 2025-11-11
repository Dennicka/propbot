"""Process-wide safe-mode coordination."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock
from time import time
from typing import Any, Mapping


LOGGER = logging.getLogger(__name__)


class SafeModeState(str, Enum):
    """Safe-mode state machine."""

    NORMAL = "normal"
    HOLD = "hold"
    KILL = "kill"


@dataclass(frozen=True)
class SafeModeStatus:
    state: SafeModeState = SafeModeState.NORMAL
    reason: str | None = None
    extra: Mapping[str, Any] | None = None
    updated_ts: float = field(default_factory=time)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": self.state.value,
            "reason": self.reason,
            "updated_ts": self.updated_ts,
        }
        if self.extra is not None:
            payload.update(dict(self.extra))
        return payload


_STATE_LOCK = RLock()
_STATUS = SafeModeStatus()


def _log_state_change(operation: str, previous: SafeModeStatus, current: SafeModeStatus) -> None:
    if previous.state == current.state and previous.reason == current.reason:
        return
    LOGGER.warning(
        "safe_mode.transition",
        extra={
            "log_module": __name__,
            "operation": operation,
            "mode": current.state.value,
            "previous_mode": previous.state.value,
            "reason": current.reason,
            "profile": (current.extra or {}).get("profile"),
            "symbol": (current.extra or {}).get("symbol"),
            "venue": (current.extra or {}).get("venue"),
            "notional": (current.extra or {}).get("notional"),
            "limit": (current.extra or {}).get("limit"),
        },
    )


def _set_status(new_status: SafeModeStatus, *, operation: str) -> SafeModeStatus:
    global _STATUS
    with _STATE_LOCK:
        previous = _STATUS
        if previous.state == SafeModeState.KILL and new_status.state != SafeModeState.KILL:
            return previous
        if previous.state == new_status.state and previous.reason == new_status.reason:
            return previous
        _STATUS = new_status
    _log_state_change(operation, previous, new_status)
    return new_status


def enter_hold(reason: str, extra: Mapping[str, Any] | None = None) -> SafeModeStatus:
    """Engage HOLD mode while keeping the process alive."""

    status = SafeModeStatus(
        state=SafeModeState.HOLD,
        reason=reason,
        extra=dict(extra or {}),
    )
    return _set_status(status, operation="enter_hold")


def enter_kill(reason: str, extra: Mapping[str, Any] | None = None) -> SafeModeStatus:
    """Enter KILL mode disabling all trading."""

    status = SafeModeStatus(
        state=SafeModeState.KILL,
        reason=reason,
        extra=dict(extra or {}),
    )
    return _set_status(status, operation="enter_kill")


def is_trading_allowed() -> bool:
    """Return ``True`` only when safe-mode is NORMAL."""

    return get_safe_mode_state().state is SafeModeState.NORMAL


def is_opening_allowed() -> bool:
    """Return ``True`` if opening new positions is permitted."""

    return get_safe_mode_state().state is SafeModeState.NORMAL


def is_closure_allowed() -> bool:
    """Return ``True`` when closing positions is allowed."""

    return get_safe_mode_state().state is not SafeModeState.KILL


def get_safe_mode_state() -> SafeModeStatus:
    """Return current safe-mode status."""

    with _STATE_LOCK:
        return _STATUS


def reset_safe_mode_for_tests() -> None:
    """Reset safe-mode to NORMAL (for test suites)."""

    global _STATUS
    with _STATE_LOCK:
        _STATUS = SafeModeStatus()


__all__ = [
    "SafeModeState",
    "SafeModeStatus",
    "enter_hold",
    "enter_kill",
    "is_trading_allowed",
    "is_opening_allowed",
    "is_closure_allowed",
    "get_safe_mode_state",
    "reset_safe_mode_for_tests",
]
