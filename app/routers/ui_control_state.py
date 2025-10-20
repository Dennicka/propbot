from __future__ import annotations
from dataclasses import asdict

from fastapi import APIRouter

from ..services.runtime import get_state

router = APIRouter()


@router.get("/control-state")
def control_state() -> dict:
    state = get_state()
    return {
        "mode": state.control.mode,
        "safe_mode": state.control.safe_mode,
        "two_man_rule": state.control.two_man_rule,
        "approvals": state.control.approvals,
        "preflight_passed": state.control.preflight_passed,
        "last_preflight_ts": state.control.last_preflight_ts,
        "guards": {name: guard.status for name, guard in state.guards.items()},
    }


@router.get("/state")
def runtime_state() -> dict:
    state = get_state()
    guards = {name: asdict(guard) for name, guard in state.guards.items()}
    slo = dict(state.metrics.slo)
    incidents = list(state.incidents)
    return {
        "guards": guards,
        "slo": slo,
        "incidents": incidents,
        "flags": state.control.flags,
    }
