from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from typing import List

from ..services import arbitrage

router = APIRouter()


class Opportunity(BaseModel):
    symbol: str
    venue: str
    edge_bps: float


@router.get("/opportunities", response_model=List[Opportunity])
def opportunities() -> List[Opportunity]:
    result: List[Opportunity] = []
    for edge in arbitrage.current_edges():
        long = edge["pair"]["long"]
        short = edge["pair"]["short"]
        result.append(
            Opportunity(symbol=long["symbol"], venue=long["venue"], edge_bps=edge["net_edge_bps"])
        )
        result.append(
            Opportunity(symbol=short["symbol"], venue=short["venue"], edge_bps=edge["net_edge_bps"])
        )
    return result
