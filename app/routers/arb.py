from __future__ import annotations

from typing import Dict, List, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, root_validator

from ..services import arbitrage
from ..services.runtime import get_state, set_last_execution, set_last_plan

router = APIRouter()


class PreviewRequest(BaseModel):
    symbol: str
    pair: str | None = None
    notional: float | None = None
    slippage_bps: int | None = Field(default=None, alias="used_slippage_bps")

    @root_validator(pre=True)
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


class PlanLegModel(BaseModel):
    ex: str = Field(..., description="Exchange identifier")
    side: Literal["buy", "sell"]
    px: float = Field(..., description="Execution price")
    qty: float = Field(..., description="Quantity in contract units")
    fee_usdt: float = Field(..., description="Estimated taker fee in USDT")


class PlanModel(BaseModel):
    symbol: str
    notional: float
    viable: bool
    legs: List[PlanLegModel] = Field(default_factory=list)
    est_pnl_usdt: float = 0.0
    est_pnl_bps: float = 0.0
    used_fees_bps: Dict[str, int] = Field(default_factory=dict)
    used_slippage_bps: int = 0
    reason: str | None = None


@router.post("/preview")
async def preview(request: PreviewRequest) -> dict:
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


@router.post("/execute")
async def execute(plan_body: PlanModel) -> dict:
    state = get_state()
    if state.control.safe_mode:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="SAFE_MODE blocks execution")
    plan = arbitrage.plan_from_payload(plan_body.model_dump())
    if not plan.viable:
        detail = plan.reason or "plan not viable"
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    try:
        report = await arbitrage.execute_plan_async(plan)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    report_dict = report.as_dict()
    set_last_execution(report_dict)
    return report_dict
