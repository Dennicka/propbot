from __future__ import annotations

from typing import Dict, Mapping

from ..risk.core import FeatureFlags
from ..risk.daily_loss import get_daily_loss_cap_state
from ..watchdog.exchange_watchdog import get_exchange_watchdog
from .runtime import get_state


BADGE_ON = "ON"
BADGE_OFF = "OFF"
BADGE_OK = "OK"
BADGE_BREACH = "BREACH"
BADGE_DEGRADED = "DEGRADED"
BADGE_AUTO_HOLD = "AUTO_HOLD"


def _auto_trade_status() -> str:
    state = get_state()
    control = getattr(state, "control", None)
    if control is None:
        return BADGE_OFF
    auto_loop = bool(getattr(control, "auto_loop", False))
    if not auto_loop:
        return BADGE_OFF
    if getattr(control, "dry_run_mode", False):
        return BADGE_OFF
    if FeatureFlags.dry_run_mode():
        return BADGE_OFF
    return BADGE_ON


def _risk_checks_status() -> str:
    return BADGE_ON if FeatureFlags.risk_checks_enabled() else BADGE_OFF


def _daily_loss_status() -> str:
    snapshot = get_daily_loss_cap_state()
    if not isinstance(snapshot, Mapping):
        return BADGE_OK
    breached = bool(snapshot.get("breached"))
    return BADGE_BREACH if breached else BADGE_OK


def _watchdog_status() -> str:
    watchdog = get_exchange_watchdog()
    snapshot = watchdog.get_state()
    entries = snapshot.values() if isinstance(snapshot, Mapping) else []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        status_text = str(entry.get("status") or "").strip().upper()
        if status_text == BADGE_AUTO_HOLD or bool(entry.get("auto_hold")):
            return BADGE_AUTO_HOLD
    if watchdog.overall_ok():
        return BADGE_OK
    return BADGE_DEGRADED


def get_runtime_badges() -> Dict[str, str]:
    """Return the aggregated runtime status badges for operator views."""

    return {
        "auto_trade": _auto_trade_status(),
        "risk_checks": _risk_checks_status(),
        "daily_loss": _daily_loss_status(),
        "watchdog": _watchdog_status(),
    }


__all__ = ["get_runtime_badges"]
