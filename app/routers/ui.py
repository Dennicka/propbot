from __future__ import annotations

import asyncio
import csv
import io
import os
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, conint, confloat

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
    HoldActiveError,
    apply_control_patch,
    approve_resume,
    control_as_dict,
    engage_safety_hold,
    get_last_plan,
    get_safety_status,
    get_state,
    is_hold_active,
    record_resume_request,
    set_loop_config,
    set_mode,
    set_open_orders,
)
from ..services import portfolio, risk
from ..services.hedge_log import read_entries
from ..security import require_token
from positions import list_positions
from ..utils import redact_sensitive_data

router = APIRouter(prefix="/api/ui", tags=["ui"])


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class HoldPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str | None = Field(default=None, description="Reason for triggering hold")
    requested_by: str | None = Field(default=None, description="Operator requesting hold")


class ResumeRequestPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(..., description="Why trading should resume")
    requested_by: str | None = Field(default=None, description="Operator requesting resume")


class ResumeConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    token: str = Field(..., description="Approval token for confirming resume")
    actor: str | None = Field(default=None, description="Operator confirming resume")


class SecretUpdate(BaseModel):
    auto_loop: bool | None = Field(default=None, description="Enable or disable auto loop")
    pair: str | None = Field(default=None, description="Target symbol override")
    venues: list[str] | None = Field(default=None, description="Venues participating in the loop")
    notional_usdt: float | None = Field(default=None, description="Order notional in USDT")


class ControlPatchPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    min_spread_bps: confloat(ge=0.0) | None = Field(default=None, description="Minimum spread in bps")
    max_slippage_bps: conint(ge=0, le=1_000) | None = Field(default=None, description="Maximum allowed slippage in bps")
    order_notional_usdt: confloat(gt=0.0) | None = Field(default=None, description="Order notional in USDT")
    safe_mode: bool | None = None
    dry_run_only: bool | None = Field(default=None, description="Restrict execution to dry-run")
    two_man_rule: bool | None = Field(default=None, description="Require two-man approval")
    auto_loop: bool | None = Field(default=None, description="Toggle auto loop")
    loop_pair: str | None = Field(default=None, description="Override loop symbol")
    loop_venues: list[str] | None = Field(default=None, description="Override loop venues")


class CancelAllPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: str | None = Field(default=None, description="Limit cancel-all to a specific venue")


class CloseExposurePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: str | None = Field(default=None, description="Venue of the position to flatten")
    symbol: str | None = Field(default=None, description="Symbol of the position to flatten")


DEFAULT_EVENT_LIMIT = 100


def _event_page(*, offset: int = 0, limit: int = DEFAULT_EVENT_LIMIT, order: str = "desc") -> dict:
    try:
        page = ledger.fetch_events_page(offset=offset, limit=limit, order=order)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return page


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
    response = {
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
    return redact_sensitive_data(response)


@router.get("/positions")
async def hedge_positions() -> dict:
    return {"positions": list_positions()}


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
    return redact_sensitive_data(_secret_payload(state))


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


def _clean_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@router.get("/events")
async def events(
    offset: int = Query(0, ge=0),
    limit: int = Query(DEFAULT_EVENT_LIMIT, ge=1, le=1_000),
    order: str = Query("desc"),
    venue: str | None = Query(None),
    symbol: str | None = Query(None),
    level: str | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    search: str | None = Query(None),
) -> dict:
    try:
        page = ledger.fetch_events_page(
            offset=offset,
            limit=limit,
            order=order,
            venue=_clean_str(venue),
            symbol=_clean_str(symbol),
            level=_clean_str(level),
            since=since,
            until=until,
            search=_clean_str(search),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return page


def _events_csv(items: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ts", "venue", "type", "level", "symbol", "message"])
    for item in items:
        writer.writerow(
            [
                item.get("ts", ""),
                item.get("venue", "") or "",
                item.get("type", "") or "",
                item.get("level", "") or "",
                item.get("symbol", "") or "",
                item.get("message", "") or "",
            ]
        )
    return output.getvalue()


@router.get("/events/export")
async def events_export(
    format: str = Query("csv"),
    offset: int = Query(0, ge=0),
    limit: int = Query(1_000, ge=1, le=1_000),
    order: str = Query("desc"),
    venue: str | None = Query(None),
    symbol: str | None = Query(None),
    level: str | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    search: str | None = Query(None),
) -> Response:
    fmt = (format or "csv").strip().lower()
    if fmt not in {"csv", "json"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="format must be csv or json")
    try:
        page = ledger.fetch_events_page(
            offset=offset,
            limit=limit,
            order=order,
            venue=_clean_str(venue),
            symbol=_clean_str(symbol),
            level=_clean_str(level),
            since=since,
            until=until,
            search=_clean_str(search),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    items = page["items"]
    if fmt == "json":
        return JSONResponse(content=items)
    csv_body = _events_csv(items)
    response = Response(content=csv_body, media_type="text/csv")
    response.headers["Content-Disposition"] = 'attachment; filename="events.csv"'
    return response


def _format_decimal(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


@router.get("/portfolio/export")
async def portfolio_export(format: str = Query("csv")) -> Response:
    fmt = (format or "csv").strip().lower()
    if fmt not in {"csv", "json"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="format must be csv or json")
    snapshot = await portfolio.snapshot()
    positions_payload = [
        {
            "venue": position.venue,
            "symbol": position.symbol,
            "venue_type": position.venue_type,
            "qty": position.qty,
            "notional": position.notional,
            "entry": position.entry_px,
            "mark": position.mark_px,
            "upnl": position.upnl,
            "rpnl": position.rpnl,
        }
        for position in snapshot.positions
    ]
    balances_payload = [
        {
            "venue": balance.venue,
            "asset": balance.asset,
            "free": balance.free,
            "locked": balance.total - balance.free,
            "total": balance.total,
        }
        for balance in snapshot.balances
    ]
    if fmt == "json":
        return JSONResponse(
            content={
                "positions": positions_payload,
                "balances": balances_payload,
                "pnl_totals": snapshot.pnl_totals,
                "notional_total": snapshot.notional_total,
            }
        )
    lines: list[str] = ["[positions]"]
    lines.append("venue,symbol,qty,notional,entry,mark,upnl,rpnl")
    for entry in positions_payload:
        lines.append(
            ",".join(
                [
                    entry.get("venue", "") or "",
                    entry.get("symbol", "") or "",
                    _format_decimal(entry.get("qty")),
                    _format_decimal(entry.get("notional")),
                    _format_decimal(entry.get("entry")),
                    _format_decimal(entry.get("mark")),
                    _format_decimal(entry.get("upnl")),
                    _format_decimal(entry.get("rpnl")),
                ]
            )
        )
    lines.append("")
    lines.append("[balances]")
    lines.append("venue,asset,free,locked,total")
    for entry in balances_payload:
        lines.append(
            ",".join(
                [
                    entry.get("venue", "") or "",
                    entry.get("asset", "") or "",
                    _format_decimal(entry.get("free")),
                    _format_decimal(entry.get("locked")),
                    _format_decimal(entry.get("total")),
                ]
            )
        )
    csv_body = "\n".join(lines) + "\n"
    response = Response(content=csv_body, media_type="text/csv")
    response.headers["Content-Disposition"] = 'attachment; filename="portfolio.csv"'
    return response


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
async def hold(payload: HoldPayload | None = None) -> dict:
    reason = (payload.reason.strip() if payload and payload.reason else "manual_hold")
    requested_by = payload.requested_by if payload else None
    await hold_loop()
    engage_safety_hold(reason, source="ui")
    set_mode("HOLD")
    ledger.record_event(
        level="INFO",
        code="mode_change",
        payload={"mode": "HOLD", "reason": reason, "requested_by": requested_by or "ui"},
    )
    state = get_state()
    safety = get_safety_status()
    return {"mode": state.control.mode, "hold_active": safety.get("hold_active", False), "safety": safety, "ts": _ts()}


@router.post("/resume-request")
async def resume_request(payload: ResumeRequestPayload) -> dict:
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="reason_required")
    if not is_hold_active():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="hold_not_active")
    request_snapshot = record_resume_request(reason, requested_by=payload.requested_by)
    ledger.record_event(
        level="INFO",
        code="resume_requested",
        payload={"reason": reason, "requested_by": payload.requested_by or "ui"},
    )
    return {
        "resume_request": request_snapshot,
        "hold_active": True,
        "ts": _ts(),
    }


@router.post("/resume-confirm")
async def resume_confirm(payload: ResumeConfirmPayload) -> dict:
    if not is_hold_active():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="hold_not_active")
    safety_snapshot = get_safety_status()
    resume_info = safety_snapshot.get("resume_request")
    if not resume_info or resume_info.get("pending") is False:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="resume_request_missing")
    expected_token = os.getenv("APPROVE_TOKEN")
    if not expected_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="approve_token_missing")
    if not secrets.compare_digest(payload.token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    result = approve_resume(actor=payload.actor)
    safety = get_safety_status()
    ledger.record_event(
        level="INFO",
        code="resume_confirmed",
        payload={
            "actor": payload.actor or "ui",
            "hold_cleared": result.get("hold_cleared", False),
            "reason": resume_info.get("reason"),
        },
    )
    return {
        "hold_cleared": result.get("hold_cleared", False),
        "hold_active": safety.get("hold_active", False),
        "safety": safety,
        "ts": _ts(),
    }


@router.post("/resume")
async def resume() -> dict:
    if is_hold_active():
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="hold_active")
    state = get_state()
    if state.control.safe_mode:
        raise HTTPException(status_code=403, detail="SAFE_MODE enabled; disable before resume")
    await resume_loop()
    set_mode("RUN")
    ledger.record_event(level="INFO", code="mode_change", payload={"mode": "RUN"})
    state = get_state()
    safety = get_safety_status()
    return {"mode": state.control.mode, "hold_active": safety.get("hold_active", False), "ts": _ts()}


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
    try:
        result = await cancel_all_orders(venue=venue)
    except HoldActiveError as exc:
        safety = get_safety_status()
        detail = {"error": exc.reason, "reason": safety.get("hold_reason")}
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=detail) from exc
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
    try:
        result = await cancel_all_orders()
    except HoldActiveError as exc:
        safety = get_safety_status()
        detail = {"error": exc.reason, "reason": safety.get("hold_reason")}
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=detail) from exc
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
    if is_hold_active():
        safety = get_safety_status()
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={"error": "hold_active", "reason": safety.get("hold_reason")},
        )
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
@router.get("/hedge/log")
async def hedge_log(request: Request, limit: int = Query(100, ge=1, le=1_000)) -> dict:
    require_token(request)
    return {"entries": read_entries(limit=limit)}

