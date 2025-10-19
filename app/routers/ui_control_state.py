from __future__ import annotations
from fastapi import APIRouter

from ..services.runtime import get_state

router = APIRouter()


@router.get("/control-state")
def control_state() -> dict:
    state = get_state()
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
        "guards": {name: guard.status for name, guard in state.guards.items()},
    }
