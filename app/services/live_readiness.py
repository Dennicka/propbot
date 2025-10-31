from __future__ import annotations

from typing import Dict, List

from .runtime_badges import BADGE_AUTO_HOLD, BADGE_BREACH, get_runtime_badges


def compute_readiness() -> Dict[str, object]:
    """Return live readiness payload based on runtime safety badges."""

    badges = get_runtime_badges()
    reasons: List[str] = []

    if badges.get("watchdog") == BADGE_AUTO_HOLD:
        reasons.append("watchdog:auto_hold")
    if badges.get("daily_loss") == BADGE_BREACH:
        reasons.append("daily_loss:breach")

    ok = not reasons
    return {"ok": ok, "reasons": reasons}


__all__ = ["compute_readiness"]
