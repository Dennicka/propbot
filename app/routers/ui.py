from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import ledger
from ..services.loop import (
    cancel_all_orders,
    hold_loop,
    loop_snapshot,
    reset_loop,
    resume_loop,
    stop_loop,
)
from ..services.runtime import (
    get_last_plan,
    get_state,
    set_loop_config,
    set_mode,
    set_open_orders,
)
from ..services import portfolio

router = APIRouter(prefix="/api/ui", tags=["ui"])


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class SecretUpdate(BaseModel):
    auto_loop: bool | None = Field(default=None, description="Enable or disable auto loop")
    pair: str | None = Field(default=None, description="Target symbol override")
    venues: list[str] | None = Field(default=None, description="Venues participating in the loop")
    notional_usdt: float | None = Field(default=None, description="Order notional in USDT")


@router.get("/state")
async def runtime_state() -> dict:
    state = get_state()
    (exposures, pnl), open_orders, positions = await asyncio.gather(
        portfolio.snapshot(),
        asyncio.to_thread(ledger.fetch_open_orders),
        asyncio.to_thread(ledger.fetch_positions),
    )
    set_open_orders(open_orders)
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
        "open_orders": open_orders,
        "positions": positions,
        "recon_status": {"status": "ok", "last_run_ts": _ts()},
        "last_plan": dryrun.last_plan if dryrun else None,
        "last_execution": dryrun.last_execution if dryrun else None,
        "events": ledger.fetch_events(20),
        "loop": loop_snapshot(),
        "loop_config": state.loop_config.as_dict(),
    }


def _secret_payload(state) -> dict:
    loop_info = loop_snapshot()
    pair = state.control.loop_pair
    if not pair:
        last_plan = loop_info.get("last_plan") if isinstance(loop_info, dict) else None
        if isinstance(last_plan, dict):
            pair = str(last_plan.get("symbol") or "").upper() or None
    venues = list(state.control.loop_venues)
    if not venues:
        venues = ["binance-um", "okx-perp"]
    return {
        "auto_loop": bool(state.control.auto_loop),
        "pair": pair or "BTCUSDT",
        "venues": venues,
        "notional_usdt": state.control.order_notional_usdt,
        "loop": loop_info,
    }


@router.get("/secret")
async def secret_state() -> dict:
    state = get_state()
    return _secret_payload(state)


@router.post("/secret")
async def update_secret(payload: SecretUpdate) -> dict:
    state = get_state()
    if payload.auto_loop is not None:
        state.control.auto_loop = bool(payload.auto_loop)
    if payload.pair is not None:
        state.control.loop_pair = payload.pair.upper() if payload.pair else None
    if payload.venues is not None:
        state.control.loop_venues = [str(entry) for entry in payload.venues]
    if payload.notional_usdt is not None:
        state.control.order_notional_usdt = float(payload.notional_usdt)
    set_loop_config(
        pair=state.control.loop_pair,
        venues=state.control.loop_venues,
        notional_usdt=state.control.order_notional_usdt,
    )
    return _secret_payload(state)


@router.get("/orders")
async def orders_snapshot() -> dict:
    open_orders, positions, fills = await asyncio.gather(
        asyncio.to_thread(ledger.fetch_open_orders),
        asyncio.to_thread(ledger.fetch_positions),
        asyncio.to_thread(ledger.fetch_recent_fills, 20),
    )
    set_open_orders(open_orders)
    return {
        "open_orders": open_orders,
        "positions": positions,
        "fills": fills,
    }


@router.post("/hold")
async def hold() -> dict:
    await hold_loop()
    set_mode("HOLD")
    ledger.record_event(level="INFO", code="mode_change", payload={"mode": "HOLD"})
    state = get_state()
    return {"mode": state.control.mode, "ts": _ts()}


@router.post("/resume")
async def resume() -> dict:
    state = get_state()
    if state.control.safe_mode:
        raise HTTPException(status_code=403, detail="SAFE_MODE enabled; disable before resume")
    await resume_loop()
    set_mode("RUN")
    ledger.record_event(level="INFO", code="mode_change", payload={"mode": "RUN"})
    state = get_state()
    return {"mode": state.control.mode, "ts": _ts()}


@router.post("/stop")
async def stop() -> dict:
    loop_state = await stop_loop()
    ledger.record_event(level="INFO", code="loop_stop_requested", payload={"status": loop_state.status})
    return {"loop": loop_state.as_dict(), "ts": _ts()}


@router.post("/reset")
async def reset() -> dict:
    await hold_loop()
    loop_state = await reset_loop()
    set_mode("HOLD")
    ledger.record_event(level="INFO", code="loop_reset", payload={"mode": "HOLD"})
    return {"loop": loop_state.as_dict(), "ts": _ts()}


async def _cancel_all_payload() -> dict:
    state = get_state()
    environment = str(state.control.environment or "").lower()
    if environment != "testnet":
        raise HTTPException(status_code=403, detail="Cancel-all only available on testnet")
    result = await cancel_all_orders()
    ledger.record_event(level="INFO", code="cancel_all", payload=result)
    return {"result": result, "ts": _ts()}


@router.post("/cancel_all")
async def cancel_all_ui() -> dict:
    return await _cancel_all_payload()


@router.post("/cancel-all")
async def cancel_all() -> dict:
    return await _cancel_all_payload()


@router.post("/close_exposure")
async def close_exposure() -> dict:
    state = get_state()
    runtime = state.derivatives
    if not runtime:
        raise HTTPException(status_code=404, detail="derivatives runtime unavailable")
    result = runtime.flatten_all()
    ledger.record_event(level="INFO", code="flatten_requested", payload=result)
    return {"result": result, "ts": _ts()}


@router.get("/plan/last")
async def last_plan() -> dict:
    plan = get_last_plan()
    if plan is None:
        return {"last_plan": None}
    return {"last_plan": plan}
