from __future__ import annotations

from typing import Dict, List

from fastapi import FastAPI

from ..runtime.leader_lock import acquire as leader_acquire
from ..runtime.leader_lock import feature_enabled as leader_feature_enabled
from ..runtime.leader_lock import is_leader as leader_is_leader
from .runtime import engage_safety_hold
from .runtime_badges import BADGE_AUTO_HOLD, BADGE_BREACH, get_runtime_badges
from .health_state import evaluate_health


def compute_readiness(app: FastAPI) -> Dict[str, object]:
    """Evaluate live readiness gates and surface blocking reasons."""

    badges = get_runtime_badges()
    reasons: List[str] = []

    if badges.get("watchdog") == BADGE_AUTO_HOLD:
        reasons.append("watchdog:auto_hold")
    if badges.get("daily_loss") == BADGE_BREACH:
        reasons.append("daily_loss:breach")

    leader_required = leader_feature_enabled()
    leader_flag = True
    if leader_required:
        leader_ok = leader_acquire()
        leader_flag = leader_is_leader()
        if not leader_ok:
            reasons.append("leader:not_acquired")
            engage_safety_hold("leader_lock:not_leader", source="leader_lock")
    else:
        leader_flag = True

    health_snapshot = evaluate_health(app)
    health_ok = bool(health_snapshot.get("ok"))
    journal_ok = bool(health_snapshot.get("journal_ok", True))
    config_ok = bool(health_snapshot.get("config_ok", True))

    if not health_ok:
        reasons.append("healthz:not_ok")
    if not journal_ok:
        reasons.append("journal:not_ok")
    if not config_ok:
        reasons.append("config:not_ok")

    ready = not reasons
    return {
        "ready": ready,
        "reasons": reasons,
        "leader": bool(leader_flag),
        "health_ok": health_ok,
        "journal_ok": journal_ok,
        "config_ok": config_ok,
    }


__all__ = ["compute_readiness"]
