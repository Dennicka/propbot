from __future__ import annotations
from fastapi import APIRouter

from ..services.runtime import get_state

router = APIRouter()


@router.get("/state")
def ui_state() -> dict:
    state = get_state()
    guard_details = {
        name: {
            "status": guard.status,
            "summary": guard.summary,
            "metrics": guard.metrics,
            "updated_ts": guard.updated_ts,
        }
        for name, guard in state.guards.items()
    }
    return {
        "mode": state.control.mode,
        "safe_mode": state.control.safe_mode,
        "environment": state.control.environment,
        "post_only": state.control.post_only,
        "reduce_only": state.control.reduce_only,
        "two_man_rule": state.control.two_man_rule,
        "approvals": state.control.approvals,
        "preflight_passed": state.control.preflight_passed,
        "last_preflight_ts": state.control.last_preflight_ts,
        "guards": {name: details["status"] for name, details in guard_details.items()},
        "guard_details": guard_details,
        "slo": state.metrics.slo,
        "counters": state.metrics.counters,
        "latency_samples_ms": state.metrics.latency_samples_ms[-20:],
        "incidents_open": len(state.incidents),
    }
