from __future__ import annotations

from typing import Dict, Mapping

from ..risk.core import FeatureFlags
from ..risk.daily_loss import get_daily_loss_cap_state
from ..watchdog.exchange_watchdog import get_exchange_watchdog
from .runtime import get_state, get_stuck_resolver_instance
from .status import get_partial_rebalance_summary


BADGE_ON = "ON"
BADGE_OFF = "OFF"
BADGE_OK = "OK"
BADGE_BREACH = "BREACH"
BADGE_DEGRADED = "DEGRADED"
BADGE_AUTO_HOLD = "AUTO_HOLD"
BADGE_PARTIAL = "PARTIAL"
BADGE_REBALANCING = "REBALANCING"


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


def _partial_status() -> str:
    summary = get_partial_rebalance_summary()
    if summary.get("count", 0) == 0:
        return BADGE_OK
    label = str(summary.get("label") or "PARTIAL").upper()
    if label == BADGE_REBALANCING:
        return BADGE_REBALANCING
    return BADGE_PARTIAL


def _stuck_resolver_status() -> tuple[bool, str | None]:
    resolver_instance = get_stuck_resolver_instance()
    if resolver_instance is not None:
        try:
            enabled_flag = bool(getattr(resolver_instance, "enabled"))
        except Exception:  # pragma: no cover - defensive
            enabled_flag = False
        if enabled_flag:
            try:
                badge = str(resolver_instance.get_status_badge())
            except Exception:  # pragma: no cover - defensive
                badge = ""
            if badge:
                return True, badge
        else:
            return False, None

    state = get_state()
    execution = getattr(state, "execution", None)
    resolver_state = getattr(execution, "stuck_resolver", None)
    if resolver_state is None:
        return False, None
    enabled = bool(getattr(resolver_state, "enabled", False))
    if not enabled:
        return False, None
    snapshot = {}
    try:
        snapshot = resolver_state.snapshot()
    except Exception:  # pragma: no cover - defensive
        snapshot = {}
    retries = snapshot.get("retries_last_hour")
    try:
        retries_value = int(retries)
    except (TypeError, ValueError):
        retries_value = 0
    return True, f"ON (retries 1h: {retries_value})"


def get_runtime_badges() -> Dict[str, str]:
    """Return the aggregated runtime status badges for operator views."""

    badges = {
        "auto_trade": _auto_trade_status(),
        "risk_checks": _risk_checks_status(),
        "daily_loss": _daily_loss_status(),
        "watchdog": _watchdog_status(),
        "partial_hedges": _partial_status(),
    }

    resolver_enabled, resolver_value = _stuck_resolver_status()
    if resolver_enabled and resolver_value:
        badges["stuck_resolver"] = resolver_value

    return badges


__all__ = ["get_runtime_badges"]
