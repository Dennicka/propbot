"""Helpers for automatically engaging HOLD when daily loss caps are breached."""

from __future__ import annotations

from typing import Mapping

from ..audit_log import log_operator_action
from ..metrics import slo
from ..services.runtime import engage_safety_hold, send_notifier_alert


AUTO_HOLD_ACTION = "AUTO_HOLD_DAILY_LOSS"
AUTO_HOLD_REASON = "auto_hold:daily_loss_cap"
AUTO_HOLD_AUDIT_REASON = "daily_loss_cap_breached"
AUTO_HOLD_ALERT_KIND = "auto_hold_daily_loss"


def _extract_float(snapshot: Mapping[str, object], key: str) -> float | None:
    value = snapshot.get(key)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def auto_hold_on_daily_loss_breach(
    snapshot: Mapping[str, object] | None,
    *,
    dry_run: bool,
    source: str,
) -> bool:
    """Engage a system HOLD when the daily loss cap is breached.

    Returns ``True`` when the HOLD transition was initiated as part of this call.
    """

    if dry_run:
        return False

    from .core import FeatureFlags  # Local import to avoid circular dependency

    if not FeatureFlags.enforce_daily_loss_cap() or not FeatureFlags.auto_hold_daily_loss_cap():
        return False

    payload = dict(snapshot or {})
    engaged = engage_safety_hold(AUTO_HOLD_REASON, source=source)
    if not engaged:
        return False

    slo.inc_skipped("daily_loss_cap")

    realized = _extract_float(payload, "realized_pnl_today_usdt")
    if realized is None:
        realized = _extract_float(payload, "realized_today_usdt")
    cap_value = _extract_float(payload, "max_daily_loss_usdt")
    if cap_value is None:
        cap_value = _extract_float(payload, "cap_usdt")

    log_operator_action(
        "system",
        "system",
        AUTO_HOLD_ACTION,
        details={
            "reason": AUTO_HOLD_AUDIT_REASON,
            "source": source,
            "snapshot": payload,
        },
    )

    realised_text = f"{realized:.2f}" if realized is not None else "unknown"
    cap_text = f"{cap_value:.2f}" if cap_value is not None else "unknown"
    message = "AUTO-HOLD by Daily Loss Cap " f"(realized_pnl_today={realised_text}, cap={cap_text})"
    send_notifier_alert(
        AUTO_HOLD_ALERT_KIND,
        message,
        extra={
            "reason": AUTO_HOLD_AUDIT_REASON,
            "realized_pnl_today": realized,
            "cap_usdt": cap_value,
            "source": source,
        },
    )
    return True


__all__ = [
    "AUTO_HOLD_ACTION",
    "AUTO_HOLD_REASON",
    "AUTO_HOLD_AUDIT_REASON",
    "AUTO_HOLD_ALERT_KIND",
    "auto_hold_on_daily_loss_breach",
]
