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


@router.get("/edge")
def edge_view() -> dict:
    return {"pairs": arbitrage.current_edges()}


@router.post("/preview")
def preview(body: PreviewIn) -> dict:
    edges = arbitrage.current_edges()
    report = arbitrage.run_preflight()
    return {
        "preflight": report,
        "edges": edges,
        "safe_mode": get_state().control.safe_mode,
    }


@router.post("/execute")
def execute(body: PreviewIn) -> dict:
    return arbitrage.execute_trade(body.pair, body.size, force_leg_b_fail=body.force_leg_b_fail)
