from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, conint, confloat

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
    apply_control_patch,
    control_as_dict,
    set_loop_config,
    set_mode,
    set_open_orders,
)
from ..services import portfolio, risk

router = APIRouter(prefix="/api/ui", tags=["ui"])


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class SecretUpdate(BaseModel):
    auto_loop: bool | None = Field(default=None, description="Enable or disable auto loop")
    pair: str | None = Field(default=None, description="Target symbol override")
    venues: list[str] | None = Field(default=None, description="Venues participating in the loop")
    notional_usdt: float | None = Field(default=None, description="Order notional in USDT")


class ControlPatchPayload(BaseModel):
    min_spread_bps: confloat(ge=0.0) | None = Field(default=None, description="Minimum spread in bps")
    max_slippage_bps: conint(ge=0, le=1_000) | None = Field(default=None, description="Maximum allowed slippage in bps")
    order_notional_usdt: confloat(gt=0.0) | None = Field(default=None, description="Order notional in USDT")
    safe_mode: bool | None = None
    dry_run_only: bool | None = Field(default=None, description="Restrict execution to dry-run")
    two_man_rule: bool | None = Field(default=None, description="Require two-man approval")
    auto_loop: bool | None = Field(default=None, description="Toggle auto loop")
    loop_pair: str | None = Field(default=None, description="Override loop symbol")
    loop_venues: list[str] | None = Field(default=None, description="Override loop venues")

    class Config:
        extra = "ignore"


class CancelAllPayload(BaseModel):
    venue: str | None = Field(default=None, description="Limit cancel-all to a specific venue")

    class Config:
        extra = "forbid"


class CloseExposurePayload(BaseModel):
    venue: str | None = Field(default=None, description="Venue of the position to flatten")
    symbol: str | None = Field(default=None, description="Symbol of the position to flatten")

    class Config:
        extra = "forbid"


DEFAULT_EVENT_LIMIT = 100


def _event_page(*, offset: int = 0, limit: int = DEFAULT_EVENT_LIMIT) -> dict:
    items = ledger.fetch_events(limit=limit, offset=offset)
    return {
        "items": items,
        "offset": offset,
        "limit": limit,
        "count": len(items),
        "next_offset": offset + len(items),
    }


@router.get("/state")
async def runtime_state() -> dict:
    state = get_state()
    snapshot, open_orders, positions = await asyncio.gather(
        portfolio.snapshot(),
        asyncio.to_thread(ledger.fetch_open_orders),
        asyncio.to_thread(ledger.fetch_positions),
    )
    set_open_orders(open_orders)
    risk_state = risk.refresh_runtime_state(snapshot=snapshot, open_orders=open_orders)
    risk_payload = risk_state.as_dict()
    risk_blocked = bool(risk_state.breaches)
    risk_reasons = [breach.detail or breach.limit for breach in risk_state.breaches]
    dryrun = state.dryrun
    control_snapshot = control_as_dict()
    return {
        "mode": state.control.mode,
        "flags": state.control.flags,
        "safe_mode": state.control.safe_mode,
        "dry_run": state.control.dry_run,
        "two_man_rule": state.control.two_man_rule,
        "control": control_snapshot,
        "incidents": list(state.incidents),
        "metrics": {
            "counters": dict(state.metrics.counters),
            "latency_samples_ms": list(state.metrics.latency_samples_ms),
        },
        "exposures": snapshot.exposures(),
        "pnl": dict(snapshot.pnl_totals),
        "portfolio": snapshot.as_dict(),
        "open_orders": open_orders,
        "positions": positions,
        "recon_status": {"status": "ok", "last_run_ts": _ts()},
        "last_plan": dryrun.last_plan if dryrun else None,
        "last_execution": dryrun.last_execution if dryrun else None,
        "events": _event_page(limit=DEFAULT_EVENT_LIMIT, offset=0),
        "loop": loop_snapshot(),
        "loop_config": state.loop_config.as_dict(),
        "risk": risk_payload,
        "risk_blocked": risk_blocked,
        "risk_reasons": risk_reasons,
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


@router.patch("/control")
async def patch_control(payload: ControlPatchPayload) -> dict:
    state = get_state()
    environment = str(state.control.environment or "").lower()
    if environment not in {"paper", "testnet"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Control patch allowed only in paper/testnet environments",
        )
    if not state.control.safe_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="SAFE_MODE must be enabled to modify control",
        )
    payload_dict = payload.model_dump(exclude_unset=True)
    try:
        control, changes = apply_control_patch(payload_dict)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if changes:
        ledger.record_event(level="INFO", code="control_patch", payload={"changes": changes})
    return {"control": control_as_dict(), "changes": changes}


@router.get("/events")
async def events(offset: int = Query(0, ge=0), limit: int = Query(DEFAULT_EVENT_LIMIT, ge=1, le=500)) -> dict:
    return _event_page(offset=offset, limit=limit)


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


async def _cancel_all_payload(request: CancelAllPayload | None = None) -> dict:
    state = get_state()
    environment = str(state.control.environment or "").lower()
    if environment != "testnet":
        raise HTTPException(status_code=403, detail="Cancel-all only available on testnet")
    venue = request.venue if request else None
    result = await cancel_all_orders(venue=venue)
    event_payload = dict(result)
    if venue:
        event_payload["venue"] = venue
    ledger.record_event(level="INFO", code="cancel_all", payload=event_payload)
    return {"result": result, "ts": _ts()}


@router.post("/cancel_all")
async def cancel_all_ui(payload: CancelAllPayload | None = None) -> dict:
    return await _cancel_all_payload(payload)


@router.post("/cancel-all")
async def cancel_all(payload: CancelAllPayload | None = None) -> dict:
    return await _cancel_all_payload(payload)


@router.post("/kill")
async def kill_switch() -> dict:
    state = get_state()
    state.control.safe_mode = True
    set_mode("HOLD")
    await hold_loop()
    result = await cancel_all_orders()
    ledger.record_event(level="CRITICAL", code="kill_switch", payload=result)
    risk.refresh_runtime_state()
    return {
        "ts": _ts(),
        "result": result,
        "safe_mode": True,
        "mode": state.control.mode,
    }


@router.post("/close_exposure")
async def close_exposure(payload: CloseExposurePayload | None = None) -> dict:
    state = get_state()
    runtime = state.derivatives
    if not runtime:
        raise HTTPException(status_code=404, detail="derivatives runtime unavailable")
    result = runtime.flatten_all()
    event_payload = dict(result)
    if payload and (payload.venue or payload.symbol):
        event_payload.update({
            "venue": payload.venue,
            "symbol": payload.symbol,
        })
    ledger.record_event(level="INFO", code="flatten_requested", payload=event_payload)
    return {"result": result, "ts": _ts()}


@router.get("/plan/last")
async def last_plan() -> dict:
    plan = get_last_plan()
    if plan is None:
        return {"last_plan": None}
    return {"last_plan": plan}
