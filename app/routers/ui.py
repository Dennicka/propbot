from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, conint, confloat

from .. import ledger
from ..metrics import set_auto_trade_state
from ..audit_log import list_recent_operator_actions, log_operator_action
from ..dashboard_helpers import render_dashboard_response
from ..version import APP_VERSION
from ..capital_manager import get_capital_manager
from ..pnl_report import DailyPnLReporter
from ..rbac import Action, can_execute_action
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
from ..services.runtime_badges import get_runtime_badges
from ..services import approvals_store, portfolio, risk, risk_guard
from ..services.audit_log import list_recent_events as list_audit_log_events
from ..services.hedge_log import read_entries
from ..security import is_auth_enabled, require_token
from positions import list_open_positions, list_positions
from ..risk_snapshot import build_risk_snapshot
from ..risk.daily_loss import get_daily_loss_cap_state
from ..risk.accounting import (
    get_risk_snapshot as get_risk_accounting_snapshot,
    reset_strategy_budget_usage,
)
from ..strategy_budget import get_strategy_budget_manager
from ..strategy.pnl_tracker import get_strategy_pnl_tracker
from ..strategy_risk import get_strategy_risk_manager
from ..services.strategy_status import build_strategy_status
from ..orchestrator import orchestrator
from ..services.positions_view import build_positions_snapshot
from ..utils import redact_sensitive_data
from ..utils.operators import OperatorIdentity, resolve_operator_identity
from pnl_history_store import list_recent as list_recent_snapshots
from services import adaptive_risk_advisor
from services.audit_snapshot import get_recent_audit_snapshot
from services.daily_reporter import load_latest_report
from services.snapshotter import create_snapshot


def _emit_ops_alert(kind: str, text: str, extra: dict | None = None) -> None:
    try:
        from ..opsbot.notifier import emit_alert
    except Exception:
        return
    try:
        emit_alert(kind=kind, text=text, extra=extra or None)
    except Exception:
        pass

router = APIRouter(prefix="/api/ui", tags=["ui"])


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/capital")
def capital_snapshot(request: Request) -> dict[str, Any]:
    """Return the capital manager snapshot (read-only)."""

    require_token(request)
    manager = get_capital_manager()
    return manager.snapshot()


@router.get("/runtime_badges")
def runtime_badges(request: Request) -> dict[str, str]:
    """Expose compact runtime badge statuses for operator views."""

    require_token(request)
    return get_runtime_badges()


@router.get("/strategy_budget")
def strategy_budget_summary(request: Request) -> dict[str, Any]:
    """Return the per-strategy budget state for viewer/operator roles."""

    token = require_token(request)
    if token is not None:
        identity = resolve_operator_identity(token)
        if not identity:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        _, role = identity
        if role not in {"viewer", "auditor", "operator"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    manager = get_strategy_budget_manager()
    snapshot = manager.snapshot()
    strategies: list[dict[str, object]] = []
    for name in sorted(snapshot):
        entry = snapshot[name]
        strategies.append(
            {
                "strategy": name,
                "max_notional_usdt": entry.get("max_notional_usdt"),
                "current_notional_usdt": entry.get("current_notional_usdt"),
                "max_open_positions": entry.get("max_open_positions"),
                "current_open_positions": entry.get("current_open_positions"),
                "blocked": bool(entry.get("blocked")),
            }
        )
    return {"strategies": strategies, "snapshot": snapshot}


@router.get("/strategy_status")
def strategy_status_summary(request: Request) -> dict[str, Any]:
    """Return merged per-strategy status including risk, pnl and budgets."""

    token = require_token(request)
    if token is not None:
        identity = resolve_operator_identity(token)
        if not identity:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        _, role = identity
        if role not in {"viewer", "auditor", "operator"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    snapshot = build_strategy_status()
    rows = [dict(entry) for entry in snapshot.values()]
    return {"strategies": rows, "snapshot": snapshot}


@router.get("/strategy_pnl")
def strategy_pnl_overview(request: Request) -> dict[str, Any]:
    """Expose rolling realised PnL aggregates per strategy."""

    require_token(request)
    tracker = get_strategy_pnl_tracker()
    snapshot = tracker.snapshot()
    strategies: list[dict[str, object]] = []
    for name, entry in snapshot.items():
        realized_today = float(entry.get("realized_today", 0.0))
        realized_7d = float(entry.get("realized_7d", 0.0))
        max_drawdown_7d = float(entry.get("max_drawdown_7d", 0.0))
        strategies.append(
            {
                "name": name,
                "realized_today": realized_today,
                "realized_7d": realized_7d,
                "max_drawdown_7d": max_drawdown_7d,
            }
        )
    strategies.sort(key=lambda item: item["realized_today"])
    return {
        "strategies": strategies,
        "simulated_excluded": tracker.exclude_simulated_entries(),
    }


@router.get("/risk_snapshot")
async def risk_snapshot(request: Request) -> dict[str, Any]:
    """Return combined portfolio and execution risk telemetry."""

    require_token(request)
    accounting_snapshot = get_risk_accounting_snapshot()
    base_snapshot = await build_risk_snapshot()
    payload = dict(base_snapshot)
    payload["accounting"] = accounting_snapshot
    return payload


@router.get("/daily_loss_status")
def daily_loss_status(request: Request) -> dict[str, Any]:
    """Return the current bot-wide daily loss cap status."""

    require_token(request)
    return get_daily_loss_cap_state()


@router.post("/budget/reset")
async def reset_strategy_budget(request: Request, payload: BudgetResetPayload) -> dict[str, object]:
    """Reset the daily budget usage for a strategy (operator only)."""

    identity = _authorize_operator_action(request, "BUDGET_RESET")
    budget_state = reset_strategy_budget_usage(payload.strategy)
    limit = budget_state.get("limit_usdt")
    used = float(budget_state.get("used_today_usdt") or 0.0)
    remaining = None if limit is None else float(limit) - used
    blocked = False
    if limit is not None:
        try:
            limit_value = float(limit)
        except (TypeError, ValueError):
            blocked = False
        else:
            blocked = used >= (limit_value - 1e-6)
            remaining = limit_value - used
    response_budget = dict(budget_state)
    response_budget["remaining_usdt"] = remaining
    response_budget["blocked_by_budget"] = blocked
    _log_operator_success(
        identity,
        "BUDGET_RESET",
        extra={"strategy": payload.strategy, "reason": payload.reason},
    )
    return {"strategy": payload.strategy, "budget": response_budget}


def _log_operator_event(
    identity: OperatorIdentity | None,
    action: str,
    *,
    status: str,
    extra: Mapping[str, Any] | None = None,
) -> None:
    name = "unknown"
    role = "unknown"
    if identity:
        name, role = identity
    details: Dict[str, Any] = {"status": status}
    if extra:
        for key, value in extra.items():
            if key == "status":
                continue
            details[key] = value
    log_operator_action(name, role, action, details=details)


def _authorize_operator_action(request: Request, action: Action) -> Optional[OperatorIdentity]:
    if not is_auth_enabled():
        return None
    token = require_token(request)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    identity = resolve_operator_identity(token)
    if not identity:
        _log_operator_event(None, action, status="forbidden")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    name, role = identity
    if not can_execute_action(role, action):
        _log_operator_event(identity, action, status="forbidden")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return identity


def _log_operator_success(
    identity: Optional[OperatorIdentity],
    action: Action,
    *,
    status: str = "approved",
    extra: Mapping[str, Any] | None = None,
) -> None:
    if not identity:
        return
    _log_operator_event(identity, action, status=status, extra=extra)


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
    correlation_id: str | None = Field(
        default=None,
        description="Idempotency key for cancel-all requests",
    )


class CloseExposurePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: str | None = Field(default=None, description="Venue of the position to flatten")
    symbol: str | None = Field(default=None, description="Symbol of the position to flatten")


class KillRequestPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str | None = Field(default=None, description="Why the kill switch should be armed")
    requested_by: str | None = Field(default=None, description="Operator initiating the request")


class KillConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(..., min_length=1, description="Approval request identifier")
    token: str = Field(..., min_length=1, description="Second-operator approval token")
    actor: str | None = Field(default=None, description="Operator confirming the kill switch")


class UnfreezeStrategyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(..., min_length=1, description="Strategy identifier")
    reason: str = Field(..., min_length=1, description="Operator supplied reason")


class UnfreezeStrategyConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(..., min_length=1, description="Approval request identifier")
    token: str = Field(..., min_length=1, description="Second-operator approval token")
    actor: str | None = Field(default=None, description="Operator confirming the unfreeze")


class SetStrategyEnabledPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(..., min_length=1, description="Strategy identifier")
    enabled: bool = Field(..., description="Whether the strategy should be enabled")
    reason: str = Field(..., min_length=1, description="Operator supplied reason for the change")


class BudgetResetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(..., min_length=1, description="Strategy identifier")
    reason: str = Field(..., min_length=1, description="Operator supplied reason for reset")


def _dashboard_token_dependency(request: Request) -> str | None:
    return require_token(request)


def _parse_dashboard_form(raw: bytes) -> dict[str, str]:
    if not raw:
        return {}
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = raw.decode("latin1", errors="ignore")
    parsed = parse_qs(decoded, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


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
    accounting_snapshot = get_risk_accounting_snapshot()
    bot_loss_cap = accounting_snapshot.get("bot_loss_cap") if isinstance(accounting_snapshot, Mapping) else None
    dryrun = state.dryrun
    control_snapshot = control_as_dict()
    response = {
        "mode": state.control.mode,
        "flags": state.control.flags,
        "safe_mode": state.control.safe_mode,
        "dry_run": state.control.dry_run,
        "dry_run_mode": state.control.dry_run_mode,
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
        "risk_accounting": accounting_snapshot,
        "bot_loss_cap": bot_loss_cap,
        "daily_loss_cap": bot_loss_cap or get_daily_loss_cap_state(),
        "autopilot": state.autopilot.as_dict(),
    }
    return redact_sensitive_data(response)


@router.get("/positions")
async def hedge_positions(request: Request) -> dict:
    """Return current hedge positions with exposure and unrealised PnL."""

    require_token(request)
    state = get_state()
    positions = list_positions()
    if not positions:
        return {"positions": [], "exposure": {}, "totals": {"unrealized_pnl_usdt": 0.0}}
    return await build_positions_snapshot(state, positions)


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def _load_open_trades() -> list[dict[str, Any]]:
    state = get_state()
    open_positions = list_open_positions()
    if not open_positions:
        return []
    snapshot = await build_positions_snapshot(state, open_positions)
    trades: list[dict[str, Any]] = []
    positions_payload = snapshot.get("positions")
    if not isinstance(positions_payload, list):
        return trades
    for position in positions_payload:
        if not isinstance(position, Mapping):
            continue
        trade_id = str(position.get("id") or "")
        pair = str(position.get("symbol") or "").upper()
        opened_ts = str(position.get("timestamp") or "")
        legs = position.get("legs") if isinstance(position.get("legs"), list) else []
        for leg in legs:
            if not isinstance(leg, Mapping):
                continue
            leg_status = str(leg.get("status") or position.get("status") or "").lower()
            if leg_status not in {"open", "partial"}:
                continue
            if bool(leg.get("simulated")):
                continue
            trades.append(
                {
                    "trade_id": trade_id,
                    "pair": pair,
                    "side": str(leg.get("side") or "").lower(),
                    "size": _coerce_float(leg.get("base_size")),
                    "entry_price": _coerce_float(leg.get("entry_price")),
                    "unrealized_pnl": _coerce_float(leg.get("unrealized_pnl_usdt")),
                    "opened_ts": str(leg.get("timestamp") or opened_ts),
                }
            )
    trades.sort(key=lambda item: (item["pair"], item["side"], item["trade_id"], item["opened_ts"]))
    return trades


@router.get("/open-trades.csv")
async def open_trades_csv(request: Request) -> Response:
    require_token(request)
    trades = await _load_open_trades()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "trade_id",
        "pair",
        "side",
        "size",
        "entry_price",
        "unrealized_pnl",
        "opened_ts",
    ])
    for trade in trades:
        writer.writerow(
            [
                trade["trade_id"],
                trade["pair"],
                trade["side"],
                _format_decimal(trade["size"]),
                _format_decimal(trade["entry_price"]),
                _format_decimal(trade["unrealized_pnl"]),
                trade["opened_ts"],
            ]
        )
    response = Response(content=output.getvalue(), media_type="text/csv")
    response.headers["Content-Disposition"] = 'attachment; filename="open-trades.csv"'
    return response


@router.get("/orchestrator_plan")
def orchestrator_plan(request: Request) -> dict:
    """Expose the orchestrator's latest scheduling plan."""

    require_token(request)
    return orchestrator.compute_next_plan()


@router.get("/pnl_history")
async def pnl_history(request: Request, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    """Return the most recent PnL / exposure snapshots."""

    require_token(request)
    snapshots = list_recent_snapshots(limit=limit)
    return {"snapshots": snapshots, "count": len(snapshots)}


@router.get("/daily_report")
async def daily_report(request: Request) -> dict[str, Any]:
    """Return the latest persisted daily report snapshot."""

    require_token(request)
    report = load_latest_report()
    if not report:
        return {"available": False}
    payload = dict(report)
    payload.setdefault("available", True)
    return payload


@router.post("/report/daily")
async def generate_daily_pnl_report(request: Request) -> dict[str, Any]:
    """Generate and persist a daily PnL/risk snapshot for audit."""

    token = require_token(request)
    identity = None
    if token:
        identity = resolve_operator_identity(token)
    if is_auth_enabled():
        if not identity or identity[1] != "operator":
            name = identity[0] if identity else "unknown"
            role = identity[1] if identity else "unknown"
            log_operator_action(
                name,
                role,
                action="report_daily_forbidden",
                details={"status": "forbidden"},
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    reporter = DailyPnLReporter()
    snapshot = reporter.build_daily_snapshot()
    reporter.write_snapshot_to_file(Path("data/daily_reports"), snapshot=snapshot)

    if identity:
        name, role = identity
        log_operator_action(
            name,
            role,
            action="report_daily_export",
            details={"status": "ok"},
        )

    return snapshot


@router.post("/snapshot")
async def generate_snapshot(request: Request) -> dict[str, Any]:
    """Persist and return a forensic snapshot for operator audits."""

    require_token(request)
    snapshot, _ = await asyncio.to_thread(create_snapshot)
    return snapshot


@router.get("/audit_log")
async def audit_log(request: Request, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    """Return the recent incident timeline for operator export."""

    require_token(request)
    events = list_audit_log_events(limit=limit)
    return {"events": events, "count": len(events)}


@router.get("/risk_advice")
async def risk_advice(request: Request) -> dict[str, Any]:
    """Return adaptive risk advisor suggestions (read-only, token-protected)."""

    require_token(request)
    state = get_state()
    safety = state.safety
    snapshots = list_recent_snapshots(limit=adaptive_risk_advisor._DEFAULT_SNAPSHOT_WINDOW * 3)
    hold_info = {
        "hold_active": safety.hold_active,
        "hold_reason": safety.hold_reason,
        "hold_since": safety.hold_since,
        "last_released_ts": safety.last_released_ts,
    }
    risk_throttled = bool(
        safety.hold_active
        and str(safety.hold_reason or "").upper().startswith(risk_guard.AUTO_THROTTLE_PREFIX)
    )
    advice = adaptive_risk_advisor.generate_risk_advice(
        snapshots,
        hold_info=hold_info,
        dry_run_mode=getattr(state.control, "dry_run_mode", False),
        risk_throttled=risk_throttled,
    )
    return advice


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
        _emit_ops_alert(
            "control_patch",
            "Control configuration updated",
            {"changes": changes},
        )
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
        set_auto_trade_state(state.control.auto_loop)
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
async def hold(request: Request, payload: HoldPayload | None = None) -> dict:
    identity = _authorize_operator_action(request, "HOLD")
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
    _emit_ops_alert(
        "mode_change",
        f"HOLD engaged: {reason}",
        {"requested_by": requested_by or "ui", "source": "ui"},
    )
    state = get_state()
    safety = get_safety_status()
    response = {"mode": state.control.mode, "hold_active": safety.get("hold_active", False), "safety": safety, "ts": _ts()}
    _log_operator_success(
        identity,
        "HOLD",
        extra={"reason": reason, "requested_by": requested_by or "ui"},
    )
    return response


@router.post("/dashboard-hold", response_class=HTMLResponse)
async def dashboard_hold_action(
    request: Request,
    token: str | None = Depends(_dashboard_token_dependency),
) -> HTMLResponse:
    form_data = _parse_dashboard_form(await request.body())
    reason = form_data.get("reason", "")
    operator = form_data.get("operator", "")
    payload = HoldPayload(reason=reason or None, requested_by=operator or "dashboard_ui")
    result = await hold(request, payload)
    hold_reason = result.get("safety", {}).get("hold_reason") or (
        payload.reason or "manual_hold"
    )
    message = f"HOLD engaged — reason: {hold_reason}"
    return await render_dashboard_response(request, token, message=message)


@router.post("/resume-request")
async def resume_request(request: Request, payload: ResumeRequestPayload) -> dict:
    identity = _authorize_operator_action(request, "RESUME_REQUEST")
    reason = payload.reason.strip()
    if not reason:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="reason_required")
    if not is_hold_active():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="hold_not_active")
    request_snapshot = record_resume_request(reason, requested_by=payload.requested_by)
    ledger.record_event(
        level="INFO",
        code="resume_requested",
        payload={
            "reason": reason,
            "requested_by": payload.requested_by or "ui",
            "request_id": request_snapshot.get("id"),
        },
    )
    response = {
        "resume_request": request_snapshot,
        "hold_active": True,
        "ts": _ts(),
    }
    _log_operator_success(
        identity,
        "RESUME_REQUEST",
        status="requested",
        extra={
            "reason": reason,
            "request_id": request_snapshot.get("id"),
            "requested_by": payload.requested_by or "ui",
        },
    )
    return response


@router.post("/dashboard-resume-request", response_class=HTMLResponse)
async def dashboard_resume_request_action(
    request: Request,
    token: str | None = Depends(_dashboard_token_dependency),
) -> HTMLResponse:
    form_data = _parse_dashboard_form(await request.body())
    reason = form_data.get("reason", "")
    operator = form_data.get("operator", "")
    payload = ResumeRequestPayload(reason=reason, requested_by=operator or "dashboard_ui")
    result = await resume_request(request, payload)
    request_id = result.get("resume_request", {}).get("id")
    message = "Resume request logged"
    if request_id:
        message += f" (approval id: {request_id})"
    message += "; awaiting second-operator approval."
    return await render_dashboard_response(
        request,
        token,
        message=message,
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.post("/dashboard-kill", response_class=HTMLResponse)
async def dashboard_kill_request_action(
    request: Request,
    token: str | None = Depends(_dashboard_token_dependency),
    *,
    operator: str = "",
    reason: str = "",
) -> HTMLResponse:
    payload = KillRequestPayload(reason=reason or None, requested_by=operator or "dashboard_ui")
    response = await kill_request(request, payload)
    if isinstance(response, Response):
        try:
            result_payload = json.loads(response.body.decode("utf-8")) if response.body else {}
        except json.JSONDecodeError:
            result_payload = {}
    else:
        result_payload = dict(response)
    request_id = result_payload.get("request_id")
    message = "Kill switch request recorded"
    if operator:
        message += f" by {operator}"
    if reason:
        message += f" (reason: {reason})"
    if request_id:
        message += f"; approval id: {request_id}"
    message += "; awaiting second-operator approval."
    return await render_dashboard_response(
        request,
        token,
        message=message,
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.post("/resume-confirm")
async def resume_confirm(request: Request, payload: ResumeConfirmPayload) -> dict:
    identity = _authorize_operator_action(request, "RESUME_APPROVE")
    if not is_hold_active():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="hold_not_active")
    safety_snapshot = get_safety_status()
    resume_info = safety_snapshot.get("resume_request")
    if not resume_info or resume_info.get("pending") is False:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="resume_request_missing")
    expected_token = os.getenv("APPROVE_TOKEN")
    if not expected_token:
        _log_operator_event(identity, "RESUME_APPROVE", status="denied", extra={"reason": "approve_token_missing"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="approve_token_missing")
    if not secrets.compare_digest(payload.token, expected_token):
        _log_operator_event(identity, "RESUME_APPROVE", status="denied", extra={"reason": "invalid_token"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    request_id = resume_info.get("id") or resume_info.get("request_id")
    result = approve_resume(request_id=request_id, actor=payload.actor)
    safety = get_safety_status()
    ledger.record_event(
        level="INFO",
        code="resume_confirmed",
        payload={
            "actor": payload.actor or "ui",
            "hold_cleared": result.get("hold_cleared", False),
            "reason": resume_info.get("reason"),
            "request_id": request_id,
        },
    )
    response = {
        "hold_cleared": result.get("hold_cleared", False),
        "hold_active": safety.get("hold_active", False),
        "safety": safety,
        "ts": _ts(),
    }
    _log_operator_success(
        identity,
        "RESUME_APPROVE",
        extra={"request_id": request_id, "reason": resume_info.get("reason"), "hold_cleared": result.get("hold_cleared", False)},
    )
    return response


@router.post("/resume")
async def resume(request: Request) -> dict:
    identity = _authorize_operator_action(request, "RESUME_EXECUTE")
    if is_hold_active():
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail="hold_active")
    state = get_state()
    if state.control.safe_mode:
        raise HTTPException(status_code=403, detail="SAFE_MODE enabled; disable before resume")
    await resume_loop()
    set_mode("RUN")
    ledger.record_event(level="INFO", code="mode_change", payload={"mode": "RUN"})
    _emit_ops_alert("mode_change", "Mode switched to RUN", {"source": "ui"})
    state = get_state()
    safety = get_safety_status()
    response = {"mode": state.control.mode, "hold_active": safety.get("hold_active", False), "ts": _ts()}
    _log_operator_success(identity, "RESUME_EXECUTE", extra={"source": "manual_resume"})
    return response


@router.post("/stop")
async def stop() -> dict:
    loop_state = await stop_loop()
    ledger.record_event(level="INFO", code="loop_stop_requested", payload={"status": loop_state.status})
    _emit_ops_alert("loop_stop_requested", "Loop stop requested", {"status": loop_state.status})
    return {"loop": loop_state.as_dict(), "ts": _ts()}


@router.post("/reset")
async def reset() -> dict:
    await hold_loop()
    loop_state = await reset_loop()
    set_mode("HOLD")
    ledger.record_event(level="INFO", code="loop_reset", payload={"mode": "HOLD"})
    _emit_ops_alert("loop_reset", "Loop reset to HOLD", {"mode": "HOLD"})
    return {"loop": loop_state.as_dict(), "ts": _ts()}


async def _cancel_all_payload(request: CancelAllPayload | None = None) -> dict:
    state = get_state()
    environment = str(state.control.environment or "").lower()
    if environment != "testnet":
        raise HTTPException(status_code=403, detail="Cancel-all only available on testnet")
    venue = request.venue if request else None
    correlation_id = request.correlation_id if request else None
    try:
        result = await cancel_all_orders(venue=venue, correlation_id=correlation_id)
    except HoldActiveError as exc:
        safety = get_safety_status()
        detail = {"error": exc.reason, "reason": safety.get("hold_reason")}
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=detail) from exc
    event_payload = dict(result)
    if venue:
        event_payload["venue"] = venue
    if correlation_id:
        event_payload["correlation_id"] = correlation_id
    ledger.record_event(level="INFO", code="cancel_all", payload=event_payload)
    _emit_ops_alert("cancel_all", "Cancel-all executed", event_payload)
    return {"result": result, "ts": _ts()}


@router.post("/cancel_all")
async def cancel_all_ui(request: Request, payload: CancelAllPayload | None = None) -> dict:
    identity = _authorize_operator_action(request, "CANCEL_ALL")
    result = await _cancel_all_payload(payload)
    _log_operator_success(identity, "CANCEL_ALL")
    return result


@router.post("/cancel-all")
async def cancel_all(request: Request, payload: CancelAllPayload | None = None) -> dict:
    identity = _authorize_operator_action(request, "CANCEL_ALL")
    result = await _cancel_all_payload(payload)
    _log_operator_success(identity, "CANCEL_ALL")
    return result


async def _execute_kill(*, reason: str | None = None, request_id: str | None = None) -> dict[str, Any]:
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
    event_payload = dict(result)
    if reason:
        event_payload["reason"] = reason
    if request_id:
        event_payload["request_id"] = request_id
    ledger.record_event(level="CRITICAL", code="kill_switch", payload=event_payload)
    _emit_ops_alert("kill_switch", "Kill switch engaged", event_payload)
    risk.refresh_runtime_state()
    response: dict[str, Any] = {
        "ts": _ts(),
        "result": result,
        "safe_mode": True,
        "mode": state.control.mode,
    }
    if reason:
        response["reason"] = reason
    if request_id:
        response["request_id"] = request_id
    return response


@router.post("/kill-request")
async def kill_request(request: Request, payload: KillRequestPayload | None = None) -> dict[str, Any]:
    identity = _authorize_operator_action(request, "KILL_REQUEST")
    reason = ""
    requested_by = None
    if payload:
        reason = (payload.reason or "").strip()
        requested_by = (payload.requested_by or "").strip() or None
    operator_name = identity[0] if identity else None
    record = approvals_store.create_request(
        "kill_switch",
        requested_by=requested_by or operator_name,
        parameters={"reason": reason} if reason else {},
    )
    _log_operator_success(
        identity,
        "KILL_REQUEST",
        status="requested",
        extra={"reason": reason, "request_id": record.get("id"), "requested_by": requested_by or operator_name},
    )
    response_payload = {
        "status": "pending",
        "request_id": record.get("id"),
        "action": record.get("action"),
        "reason": reason,
    }
    return JSONResponse(response_payload, status_code=status.HTTP_202_ACCEPTED)


@router.post("/kill")
@router.post("/kill-confirm")
async def kill_switch(request: Request, payload: KillConfirmPayload) -> dict:
    identity = _authorize_operator_action(request, "KILL_APPROVE")
    expected_token = os.getenv("APPROVE_TOKEN")
    if not expected_token:
        _log_operator_event(identity, "KILL_APPROVE", status="denied", extra={"reason": "approve_token_missing"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="approve_token_missing")
    if not secrets.compare_digest(payload.token, expected_token):
        _log_operator_event(identity, "KILL_APPROVE", status="denied", extra={"reason": "invalid_token"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    try:
        record = approvals_store.approve_request(payload.request_id, actor=payload.actor)
    except KeyError as exc:
        _log_operator_event(identity, "KILL_APPROVE", status="denied", extra={"reason": "request_not_found", "request_id": payload.request_id})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found") from exc
    except ValueError as exc:
        _log_operator_event(identity, "KILL_APPROVE", status="denied", extra={"reason": "request_not_pending", "request_id": payload.request_id})
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request_not_pending") from exc
    parameters = record.get("parameters") if isinstance(record, Mapping) else {}
    reason = None
    if isinstance(parameters, Mapping):
        raw_reason = parameters.get("reason")
        if isinstance(raw_reason, str):
            reason = raw_reason
    response = await _execute_kill(reason=reason, request_id=str(record.get("id")))
    _log_operator_success(
        identity,
        "KILL_APPROVE",
        extra={"request_id": record.get("id"), "reason": reason},
    )
    return response


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
    _emit_ops_alert("flatten_requested", "Exposure flatten requested", event_payload)
    return {"result": result, "ts": _ts()}


@router.post("/unfreeze-strategy")
def unfreeze_strategy_request(payload: UnfreezeStrategyPayload, request: Request) -> Response:
    identity = _authorize_operator_action(request, "UNFREEZE_STRATEGY_REQUEST")
    strategy = payload.strategy.strip()
    reason = payload.reason.strip()
    operator_name = identity[0] if identity else None
    record = approvals_store.create_request(
        "unfreeze_strategy",
        requested_by=operator_name,
        parameters={"strategy": strategy, "reason": reason},
    )
    _log_operator_success(
        identity,
        "UNFREEZE_STRATEGY_REQUEST",
        status="requested",
        extra={"strategy": strategy, "reason": reason, "request_id": record.get("id")},
    )
    payload_out = {
        "status": "pending",
        "request_id": record.get("id"),
        "strategy": strategy,
        "reason": reason,
    }
    return JSONResponse(payload_out, status_code=status.HTTP_202_ACCEPTED)


@router.post("/unfreeze-strategy/confirm")
def unfreeze_strategy_confirm(payload: UnfreezeStrategyConfirmPayload, request: Request) -> dict[str, object]:
    identity = _authorize_operator_action(request, "UNFREEZE_STRATEGY_APPROVE")
    expected_token = os.getenv("APPROVE_TOKEN")
    if not expected_token:
        _log_operator_event(identity, "UNFREEZE_STRATEGY_APPROVE", status="denied", extra={"reason": "approve_token_missing"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="approve_token_missing")
    if not secrets.compare_digest(payload.token, expected_token):
        _log_operator_event(identity, "UNFREEZE_STRATEGY_APPROVE", status="denied", extra={"reason": "invalid_token"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    try:
        record = approvals_store.approve_request(payload.request_id, actor=payload.actor)
    except KeyError as exc:
        _log_operator_event(
            identity,
            "UNFREEZE_STRATEGY_APPROVE",
            status="denied",
            extra={"reason": "request_not_found", "request_id": payload.request_id},
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found") from exc
    except ValueError as exc:
        _log_operator_event(
            identity,
            "UNFREEZE_STRATEGY_APPROVE",
            status="denied",
            extra={"reason": "request_not_pending", "request_id": payload.request_id},
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request_not_pending") from exc
    parameters = record.get("parameters") if isinstance(record, Mapping) else {}
    if not isinstance(parameters, Mapping):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="missing_parameters")
    strategy = parameters.get("strategy")
    reason = parameters.get("reason") or ""
    if not isinstance(strategy, str) or not strategy:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid_strategy")
    manager = get_strategy_risk_manager()
    operator_name, role = identity or ("unknown", "unknown")
    manager.unfreeze_strategy(
        strategy,
        operator_name=operator_name,
        role=role,
        reason=str(reason),
    )
    snapshot = manager.check_limits(strategy)
    response = {
        "status": "approved",
        "strategy": strategy,
        "frozen": manager.is_frozen(strategy),
        "snapshot": snapshot.get("snapshot", {}),
        "request_id": record.get("id"),
    }
    _log_operator_success(
        identity,
        "UNFREEZE_STRATEGY_APPROVE",
        extra={"strategy": strategy, "reason": reason, "request_id": record.get("id")},
    )
    return response


@router.post("/set-strategy-enabled")
async def set_strategy_enabled(request: Request) -> dict[str, object]:
    raw_body = await request.body()
    payload_data: dict[str, Any]
    parsed: dict[str, Any] | None = None
    if raw_body:
        try:
            decoded = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            decoded = raw_body.decode("latin1", errors="ignore")
        try:
            json_payload = json.loads(decoded)
        except json.JSONDecodeError:
            json_payload = None
        if isinstance(json_payload, Mapping):
            parsed = dict(json_payload)
    if parsed is None:
        payload_data = _parse_dashboard_form(raw_body)
    else:
        payload_data = parsed
    if "strategy" not in payload_data and "strategy_name" in payload_data:
        payload_data["strategy"] = payload_data["strategy_name"]
    try:
        payload = SetStrategyEnabledPayload(**payload_data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(),
        ) from exc
    identity = _authorize_operator_action(request, "SET_STRATEGY_ENABLED")
    operator_name, role = identity or ("local-dev", "operator")

    manager = get_strategy_risk_manager()
    manager.set_enabled(
        payload.strategy,
        payload.enabled,
        operator_name,
        payload.reason,
        role=role,
    )
    snapshot = manager.check_limits(payload.strategy)
    state_snapshot = snapshot.get("snapshot", {}) or {}
    response = {
        "status": "ok",
        "strategy": payload.strategy,
        "enabled": manager.is_enabled(payload.strategy),
        "frozen": bool(snapshot.get("frozen")),
        "breach": bool(snapshot.get("breach")),
        "breach_reasons": list(snapshot.get("breach_reasons", [])),
        "snapshot": state_snapshot,
        "limits": snapshot.get("limits", {}),
    }
    _log_operator_success(
        identity,
        "SET_STRATEGY_ENABLED",
        extra={
            "strategy": payload.strategy,
            "enabled": payload.enabled,
            "reason": payload.reason,
        },
    )
    return response


@router.post("/dashboard-unfreeze-strategy", response_class=HTMLResponse)
async def dashboard_unfreeze_strategy_action(
    request: Request,
    token: str | None = Depends(_dashboard_token_dependency),
) -> HTMLResponse:
    form_data = _parse_dashboard_form(await request.body())
    strategy = form_data.get("strategy", "")
    reason = form_data.get("reason", "")
    payload = UnfreezeStrategyPayload(strategy=strategy, reason=reason)
    response = unfreeze_strategy_request(payload, request)
    if isinstance(response, Response):
        try:
            result_payload = json.loads(response.body.decode("utf-8")) if response.body else {}
        except json.JSONDecodeError:
            result_payload = {}
    else:
        result_payload = dict(response)
    request_id = result_payload.get("request_id")
    message = f"Unfreeze request logged for {strategy}"
    if reason:
        message += f" (reason: {reason})"
    if request_id:
        message += f"; approval id: {request_id}"
    message += "; awaiting second-operator approval."
    return await render_dashboard_response(
        request,
        token,
        message=message,
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.get("/audit_snapshot")
async def audit_snapshot(request: Request, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    token = require_token(request)
    if token is not None:
        identity = resolve_operator_identity(token)
        if not identity:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
        _, role = identity
        if role not in {"viewer", "auditor", "operator"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    entries = get_recent_audit_snapshot(limit=limit)
    return {
        "entries": entries,
        "count": len(entries),
        "limit": limit,
        "build_version": APP_VERSION,
    }


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


@router.get("/alerts")
async def ops_alerts(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    since: str | None = Query(default=None),
) -> dict:
    """Return recent operator alerts, protected by API token."""

    require_token(request)
    try:
        from ..opsbot.notifier import get_recent_alerts
    except Exception:
        return {"alerts": []}
    alerts = get_recent_alerts(limit=limit, since=since)
    return {"alerts": alerts}


@router.get("/audit/export")
async def audit_export(
    request: Request,
    limit: int = Query(200, ge=1, le=1_000),
) -> JSONResponse:
    """Return the latest audit entries for operator export."""

    require_token(request)
    try:
        events = list_recent_operator_actions(limit=limit)
    except Exception:
        events = []
    return JSONResponse({"events": events})

