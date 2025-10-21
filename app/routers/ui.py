from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from .. import ledger
from ..services.runtime import get_last_plan, get_state, set_mode

router = APIRouter(prefix="/api/ui", tags=["ui"])


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/state")
async def runtime_state() -> dict:
    state = get_state()
    exposures, pnl = await asyncio.gather(
        asyncio.to_thread(ledger.compute_exposures),
        asyncio.to_thread(ledger.compute_pnl),
    )
    dryrun = state.dryrun
    return {
        "mode": state.control.mode,
        "flags": state.control.flags,
        "safe_mode": state.control.safe_mode,
        "dry_run": state.control.dry_run,
        "two_man_rule": state.control.two_man_rule,
        "incidents": list(state.incidents),
        "metrics": {
            "counters": dict(state.metrics.counters),
            "latency_samples_ms": list(state.metrics.latency_samples_ms),
        },
        "exposures": exposures,
        "pnl": pnl,
        "recon_status": {"status": "ok", "last_run_ts": _ts()},
        "last_plan": dryrun.last_plan if dryrun else None,
        "last_execution": dryrun.last_execution if dryrun else None,
        "events": ledger.fetch_events(20),
    }


@router.post("/hold")
async def hold() -> dict:
    set_mode("HOLD")
    ledger.record_event(level="INFO", code="mode_change", payload={"mode": "HOLD"})
    state = get_state()
    return {"mode": state.control.mode, "ts": _ts()}


@router.post("/resume")
async def resume() -> dict:
    state = get_state()
    if state.control.safe_mode:
        raise HTTPException(status_code=403, detail="SAFE_MODE enabled; disable before resume")
    set_mode("RUN")
    ledger.record_event(level="INFO", code="mode_change", payload={"mode": "RUN"})
    state = get_state()
    return {"mode": state.control.mode, "ts": _ts()}


@router.get("/plan/last")
async def last_plan() -> dict:
    plan = get_last_plan()
    if plan is None:
        return {"last_plan": None}
    return {"last_plan": plan}
