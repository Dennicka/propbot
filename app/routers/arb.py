from __future__ import annotations

from typing import Annotated, Dict, List, Literal, Mapping, Union
import os
import secrets

from fastapi import APIRouter, Body, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..services import arbitrage
from ..services.runtime import (
    HoldActiveError,
    get_last_opportunity_state,
    get_safety_status,
    get_state,
    is_hold_active,
    set_last_execution,
    set_last_opportunity_state,
    set_last_plan,
)
from positions import create_position
from services.cross_exchange_arb import check_spread, execute_hedged_trade
from services.risk_manager import can_open_new_position

router = APIRouter()


STRATEGY_NAME = "cross_exchange_arb"


def _emit_ops_alert(kind: str, text: str, extra: Mapping[str, object] | None = None) -> None:
    try:
        from ..opsbot.notifier import emit_alert
    except Exception:
        return
    try:
        emit_alert(kind=kind, text=text, extra=extra or None)
    except Exception:
        pass


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    pair: str | None = None
    notional: float | None = None
    slippage_bps: int | None = Field(default=None, alias="used_slippage_bps")

    @model_validator(mode="before")
    def _alias_pair(cls, values: Dict[str, object]) -> Dict[str, object]:
        symbol = values.get("symbol")
        pair = values.get("pair")
        if symbol and pair and str(symbol).upper() != str(pair).upper():
            raise ValueError("symbol and pair must match when both provided")
        if not symbol and pair:
            values["symbol"] = pair
        if "symbol" not in values or not values.get("symbol"):
            raise ValueError("symbol or pair is required")
        return values


class CrossPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    min_spread: float


class PlanLegModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    ex: str = Field(..., description="Exchange identifier")
    side: Literal["buy", "sell"]
    px: float = Field(..., description="Execution price")
    qty: float = Field(..., description="Quantity in contract units")
    fee_usdt: float = Field(..., description="Estimated taker fee in USDT")


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    notional: float
    viable: bool
    legs: List[PlanLegModel] = Field(default_factory=list)
    est_pnl_usdt: float = 0.0
    est_pnl_bps: float = 0.0
    used_fees_bps: Dict[str, int] = Field(default_factory=dict)
    used_slippage_bps: int = 0
    reason: str | None = None


class CrossExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    symbol: str
    min_spread: float
    notion_usdt: float
    leverage: float


class ConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str
    token: str


@router.post("/preview")
async def preview(request: PreviewRequest) -> dict:
    extras = getattr(request, "model_extra", {}) or {}
    if "min_spread" in extras and extras.get("min_spread") is not None:
        min_spread = float(extras["min_spread"])
        spread_info = check_spread(request.symbol)
        spread_value = float(spread_info["spread"])
        meets_threshold = spread_value >= min_spread
        response = {
            "symbol": request.symbol,
            "min_spread": min_spread,
            "spread": spread_value,
            "meets_min_spread": meets_threshold,
            "long_exchange": spread_info["cheap"],
            "short_exchange": spread_info["expensive"],
            "cheap_ask": spread_info["cheap_ask"],
            "expensive_bid": spread_info["expensive_bid"],
        }
        return response

    state = get_state()
    notional_value = (
        float(request.notional)
        if request.notional is not None
        else state.control.order_notional_usdt
    )
    slippage_value = (
        int(request.slippage_bps)
        if request.slippage_bps is not None
        else state.control.max_slippage_bps
    )
    plan = arbitrage.build_plan(request.symbol, notional_value, slippage_value)
    plan_dict = plan.as_dict()
    set_last_plan(plan_dict)
    return plan_dict


ExecutePayload = Annotated[
    Union[PlanModel, CrossExecuteRequest],
    Body(...),
]


@router.post("/execute")
async def execute(plan_body: ExecutePayload) -> dict:
    state = get_state()
    if is_hold_active():
        safety = get_safety_status()
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={"error": "hold_active", "reason": safety.get("hold_reason")},
        )
    if state.control.safe_mode:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="SAFE_MODE blocks execution")
    if isinstance(plan_body, CrossExecuteRequest):
        can_open, reason = can_open_new_position(plan_body.notion_usdt, plan_body.leverage)
        if not can_open:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)
        trade_result = execute_hedged_trade(
            plan_body.symbol,
            plan_body.notion_usdt,
            plan_body.leverage,
            plan_body.min_spread,
        )
        if not trade_result.get("success"):
            detail = trade_result.get("reason", "spread_below_threshold")
            if trade_result.get("hold_active"):
                safety = get_safety_status()
                raise HTTPException(
                    status_code=status.HTTP_423_LOCKED,
                    detail={"error": detail, "reason": safety.get("hold_reason")},
                )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
        simulated = bool(trade_result.get("simulated"))
        long_order = trade_result.get("long_order") or {}
        short_order = trade_result.get("short_order") or {}
        long_price = float(
            long_order.get("price")
            or long_order.get("avg_price")
            or trade_result.get("details", {}).get("cheap_mark", 0.0)
            or 0.0
        )
        short_price = float(
            short_order.get("price")
            or short_order.get("avg_price")
            or trade_result.get("details", {}).get("expensive_mark", 0.0)
            or 0.0
        )
        position = create_position(
            symbol=plan_body.symbol,
            long_venue=str(trade_result.get("cheap_exchange")),
            short_venue=str(trade_result.get("expensive_exchange")),
            notional_usdt=plan_body.notion_usdt,
            entry_spread_bps=float(trade_result.get("spread_bps", 0.0)),
            leverage=plan_body.leverage,
            entry_long_price=long_price,
            entry_short_price=short_price,
            status="simulated" if simulated else "open",
            simulated=simulated,
            legs=trade_result.get("legs"),
            strategy=STRATEGY_NAME,
        )
        trade_result["position"] = position
        alert_payload = {
            "symbol": plan_body.symbol,
            "notional_usdt": plan_body.notion_usdt,
            "leverage": plan_body.leverage,
            "spread_bps": trade_result.get("spread_bps"),
            "simulated": simulated,
            "dry_run_mode": bool(trade_result.get("dry_run_mode")),
        }
        alert_text = (
            f"Manual hedge simulated for {plan_body.symbol} (DRY_RUN_MODE)"
            if simulated
            else f"Manual hedge executed for {plan_body.symbol}"
        )
        _emit_ops_alert("manual_hedge_execute", alert_text, alert_payload)
        return trade_result

    payload = plan_body.model_dump()
    plan = arbitrage.plan_from_payload(payload)
    dry_run = bool(state.control.dry_run)
    if not plan.viable and not dry_run:
        detail = plan.reason or "plan not viable"
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    try:
        report = await arbitrage.execute_plan_async(plan)
    except HoldActiveError as exc:
        safety = get_safety_status()
        detail = {"error": exc.reason, "reason": safety.get("hold_reason")}
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=detail) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    report_dict = report.as_dict()
    report_dict.setdefault("orders", [])
    report_dict.setdefault("exposures", [])
    pnl_summary = report_dict.setdefault("pnl_summary", {})
    pnl_summary.setdefault("realized", 0.0)
    pnl_summary.setdefault("unrealized", 0.0)
    pnl_summary.setdefault("total", 0.0)
    set_last_execution(report_dict)
    return report_dict


@router.get("/opportunity")
async def last_opportunity() -> dict:
    opportunity, status_flag = get_last_opportunity_state()
    return {"last_opportunity": opportunity, "status": status_flag}


@router.post("/confirm")
async def confirm(payload: ConfirmPayload) -> dict:
    state = get_state()
    if is_hold_active():
        safety = get_safety_status()
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={"error": "hold_active", "reason": safety.get("hold_reason")},
        )
    if state.control.safe_mode:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="SAFE_MODE blocks execution")
    expected_token = os.getenv("API_TOKEN")
    if not expected_token or not secrets.compare_digest(payload.token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_token")
    opportunity, _status = get_last_opportunity_state()
    if not opportunity or str(opportunity.get("id")) != payload.opportunity_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="opportunity_not_found")
    notional = float(opportunity.get("notional_suggestion", 0.0) or 0.0)
    leverage = float(opportunity.get("leverage_suggestion", 0.0) or 0.0)
    allowed, reason = can_open_new_position(notional, leverage)
    if not allowed:
        set_last_opportunity_state(opportunity, "blocked_by_risk")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)
    min_spread = float(opportunity.get("min_spread", opportunity.get("spread", 0.0)) or 0.0)
    trade_result = execute_hedged_trade(
        str(opportunity.get("symbol")),
        notional,
        leverage,
        min_spread,
    )
    if not trade_result.get("success"):
        detail = trade_result.get("reason", "spread_below_threshold")
        set_last_opportunity_state(opportunity, "blocked_by_risk")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    simulated = bool(trade_result.get("simulated"))
    position = create_position(
        symbol=str(opportunity.get("symbol")),
        long_venue=str(trade_result.get("cheap_exchange")),
        short_venue=str(trade_result.get("expensive_exchange")),
        notional_usdt=notional,
        entry_spread_bps=float(trade_result.get("spread_bps", opportunity.get("spread_bps", 0.0))),
        leverage=leverage,
        entry_long_price=float(trade_result.get("long_order", {}).get("price", 0.0)),
        entry_short_price=float(trade_result.get("short_order", {}).get("price", 0.0)),
        status="simulated" if simulated else None,
        simulated=simulated,
        strategy=STRATEGY_NAME,
    )
    trade_result["position"] = position
    trade_result["opportunity_id"] = opportunity.get("id")
    alert_payload = {
        "symbol": opportunity.get("symbol"),
        "notional_usdt": notional,
        "leverage": leverage,
        "spread_bps": trade_result.get("spread_bps"),
        "simulated": simulated,
        "dry_run_mode": bool(trade_result.get("dry_run_mode")),
    }
    alert_text = (
        f"Manual opportunity simulated for {opportunity.get('symbol')} (DRY_RUN_MODE)"
        if simulated
        else f"Manual opportunity executed for {opportunity.get('symbol')}"
    )
    _emit_ops_alert("manual_hedge_confirm", alert_text, alert_payload)
    set_last_opportunity_state(None, "blocked_by_risk")
    return trade_result
