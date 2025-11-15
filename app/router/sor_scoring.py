"""Scoring helpers for smart order routing candidates."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Literal

VenueId = str

Side = Literal["buy", "sell"]


@dataclass(slots=True)
class RouterVenueMarketSnapshot:
    """Minimal market snapshot info used for SOR decision."""

    venue_id: VenueId
    best_bid: Decimal | None
    best_ask: Decimal | None


@dataclass(slots=True)
class RouterVenueCostEstimate:
    """Simplified per-venue cost estimate for a single order."""

    fee_rate: Decimal
    funding_rate: Decimal | None


@dataclass(slots=True)
class RouterVenueCandidate:
    """Candidate venue for routing a single order."""

    venue_id: VenueId
    side: Side
    quantity: Decimal
    notional_estimate: Decimal
    market: RouterVenueMarketSnapshot
    costs: RouterVenueCostEstimate | None

    is_healthy: bool
    risk_allowed: bool


@dataclass(slots=True)
class RouterVenueScore:
    """Scoring result for a venue candidate."""

    venue_id: VenueId
    score: Decimal
    reason: str | None = None


def score_venue_candidate(candidate: RouterVenueCandidate) -> RouterVenueScore:
    """Compute a simple score for a venue candidate."""

    if not candidate.is_healthy:
        return RouterVenueScore(
            venue_id=candidate.venue_id,
            score=Decimal("-1_000_000"),
            reason="unhealthy",
        )

    if not candidate.risk_allowed:
        return RouterVenueScore(
            venue_id=candidate.venue_id,
            score=Decimal("-1_000_000"),
            reason="risk_rejected",
        )

    price_component = Decimal("0")

    if candidate.side == "buy":
        if candidate.market.best_ask is None:
            return RouterVenueScore(
                venue_id=candidate.venue_id,
                score=Decimal("-1_000_000"),
                reason="no_ask_price",
            )
        price_component = Decimal("1_000_000") / candidate.market.best_ask
    else:
        if candidate.market.best_bid is None:
            return RouterVenueScore(
                venue_id=candidate.venue_id,
                score=Decimal("-1_000_000"),
                reason="no_bid_price",
            )
        price_component = candidate.market.best_bid

    fee_component = Decimal("0")
    if candidate.costs is not None:
        fee_component -= candidate.costs.fee_rate * Decimal("1000")
        if candidate.costs.funding_rate is not None:
            fee_component -= candidate.costs.funding_rate * Decimal("10")

    score = price_component + fee_component

    return RouterVenueScore(
        venue_id=candidate.venue_id,
        score=score,
        reason=None,
    )


def choose_best_venue(
    candidates: Iterable[RouterVenueCandidate],
) -> RouterVenueScore | None:
    """Score all candidates and return the best venue by score."""

    best: RouterVenueScore | None = None
    for candidate in candidates:
        score = score_venue_candidate(candidate)
        if best is None or score.score > best.score:
            best = score
    if best is not None and best.score < Decimal("0"):
        return None
    return best
