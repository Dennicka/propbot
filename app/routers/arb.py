from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..services import arbitrage
from ..services.runtime import get_state

router = APIRouter()


class PreviewIn(BaseModel):
    pair: str | None = None
    size: float | None = None
    force_leg_b_fail: bool = False
    symbol: str | None = None
    qty: float | None = None


class ExecuteIn(BaseModel):
    symbol: str
    qty: float
    dry_run: bool = False
    two_man_ok: bool = False


@router.get("/edge")
def edge_view() -> dict:
    return {"pairs": arbitrage.current_edges()}


@router.post("/preview")
def preview(body: PreviewIn) -> dict:
    edges = arbitrage.current_edges()
    report = arbitrage.run_preflight()
    plan_payload = {
        "symbol": body.symbol or body.pair or "",
        "qty": body.qty if body.qty is not None else body.size,
        "size": body.size,
    }
    plan = arbitrage.build_plan(plan_payload)
    return {
        "preflight": report,
        "edges": edges,
        "safe_mode": get_state().control.safe_mode,
        "plan": arbitrage.plan_as_dict(plan),
    }


@router.post("/execute")
def execute(body: ExecuteIn) -> dict:
    safe_mode = get_state().control.safe_mode
    plan = arbitrage.build_plan({"symbol": body.symbol, "qty": body.qty})
    return arbitrage.execute_plan(
        plan,
        safe_mode=safe_mode,
        two_man_ok=body.two_man_ok,
        dry_run=body.dry_run,
    )
