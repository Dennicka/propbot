"""UI endpoint exposing recent smart order router decisions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from app.router.sor_log import get_recent_router_decisions


class UiRouterCandidate(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    venue_id: str
    side: str
    quantity: Decimal
    notional_estimate: Decimal
    is_healthy: bool
    risk_allowed: bool
    price_bid: Decimal | None = None
    price_ask: Decimal | None = None
    score: Decimal
    score_reason: str | None = None


class UiRouterDecisionEntry(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ts: datetime
    symbol: str
    strategy_id: str | None = None
    runtime_profile: str

    candidates: List[UiRouterCandidate]
    chosen_venue_id: str | None = None
    chosen_score: Decimal | None = None
    reject_reason: str | None = None


router = APIRouter(tags=["ui", "router"])


@router.get("/api/ui/router-decisions", response_model=list[UiRouterDecisionEntry])
async def get_router_decisions(limit: int = Query(50, ge=1, le=200)) -> list[UiRouterDecisionEntry]:
    """Return recent SOR router decisions for debugging."""

    entries = get_recent_router_decisions(limit=limit)
    result: list[UiRouterDecisionEntry] = []
    for entry in entries:
        candidates = [
            UiRouterCandidate(
                venue_id=candidate.venue_id,
                side=str(candidate.side),
                quantity=candidate.quantity,
                notional_estimate=candidate.notional_estimate,
                is_healthy=candidate.is_healthy,
                risk_allowed=candidate.risk_allowed,
                price_bid=candidate.price_bid,
                price_ask=candidate.price_ask,
                score=candidate.score,
                score_reason=candidate.score_reason,
            )
            for candidate in entry.candidates
        ]
        result.append(
            UiRouterDecisionEntry(
                ts=entry.ts,
                symbol=entry.symbol,
                strategy_id=entry.strategy_id,
                runtime_profile=entry.runtime_profile,
                candidates=candidates,
                chosen_venue_id=entry.chosen_venue_id,
                chosen_score=entry.chosen_score,
                reject_reason=entry.reject_reason,
            )
        )
    return result


__all__ = ["router"]
