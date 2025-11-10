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
        "dry_run_mode": state.control.dry_run_mode,
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
    metrics = {
        "counters": dict(state.metrics.counters),
        "latency_samples_ms": list(state.metrics.latency_samples_ms),
    }
    risk_state = state.risk.as_dict()
    risk_blocked = bool(state.risk.breaches)
    risk_reasons = [
        breach.get("detail") or breach.get("limit") for breach in risk_state["breaches"]
    ]
    dryrun = None
    if state.dryrun:
        dryrun = {
            "last_cycle_ts": state.dryrun.last_cycle_ts,
            "last_plan": state.dryrun.last_plan,
            "last_execution": state.dryrun.last_execution,
            "last_error": state.dryrun.last_error,
            "last_spread_bps": state.dryrun.last_spread_bps,
            "last_spread_usdt": state.dryrun.last_spread_usdt,
            "last_fees_usdt": state.dryrun.last_fees_usdt,
            "cycles_completed": state.dryrun.cycles_completed,
            "poll_interval_sec": state.dryrun.poll_interval_sec,
            "min_spread_bps": state.dryrun.min_spread_bps,
        }
    return {
        "guards": guards,
        "slo": slo,
        "incidents": incidents,
        "flags": state.control.flags,
        "metrics": metrics,
        "dry_run": dryrun,
        "dry_run_mode": state.control.dry_run_mode,
        "risk": risk_state,
        "risk_blocked": risk_blocked,
        "risk_reasons": risk_reasons,
    }
