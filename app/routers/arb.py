from __future__ import annotations

from typing import Dict, List, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..services import arbitrage
from ..services.runtime import get_state

router = APIRouter()


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


@router.get("/edge")
def edge_view() -> dict:
    return {"pairs": arbitrage.current_edges()}


@router.get("/preview")
def preview(symbol: str, notional: float | None = None, slippage_bps: int | None = None) -> dict:
    state = get_state()
    notional_value = float(notional) if notional is not None else state.control.order_notional_usdt
    slippage_value = int(slippage_bps) if slippage_bps is not None else state.control.max_slippage_bps
    plan = arbitrage.build_plan(symbol, notional_value, slippage_value)
    return plan.as_dict()


@router.post("/execute")
def execute(plan_body: PlanModel) -> dict:
    state = get_state()
    if state.control.safe_mode:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="SAFE_MODE blocks execution")
    plan = arbitrage.plan_from_payload(plan_body.model_dump())
    if not plan.viable:
        detail = plan.reason or "plan not viable"
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    report = arbitrage.execute_plan(plan)
    return report.as_dict()
