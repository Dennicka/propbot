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
    best_bid_qty: Decimal | None = None
    best_ask_qty: Decimal | None = None


@dataclass(slots=True)
class RouterVenueTradingLimits:
    """Per-venue minimal trading constraints used by SOR."""

    min_notional: Decimal | None = None
    min_qty: Decimal | None = None


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
    limits: RouterVenueTradingLimits | None = None


@dataclass(slots=True)
class RouterVenueScore:
    """Scoring result for a venue candidate."""

    venue_id: VenueId
    score: Decimal
    reason: str | None = None


def estimate_effective_price(
    candidate: RouterVenueCandidate,
) -> Decimal | None:
    """Return effective price for the given candidate using a depth-aware model."""

    side = candidate.side

    if side == "buy":
        ask = candidate.market.best_ask
        ask_qty = candidate.market.best_ask_qty
        if ask is None:
            return None
        if ask_qty is None or ask_qty <= Decimal("0"):
            return None

        if candidate.quantity <= ask_qty:
            return ask

        ratio = candidate.quantity / ask_qty
        slippage_factor = Decimal("0.001") * ratio
        return ask * (Decimal("1") + slippage_factor)

    bid = candidate.market.best_bid
    bid_qty = candidate.market.best_bid_qty
    if bid is None:
        return None
    if bid_qty is None or bid_qty <= Decimal("0"):
        return None

    if candidate.quantity <= bid_qty:
        return bid

    ratio = candidate.quantity / bid_qty
    slippage_factor = Decimal("0.001") * ratio
    return bid * (Decimal("1") - slippage_factor)


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

    if candidate.limits is not None:
        min_notional = candidate.limits.min_notional
        min_qty = candidate.limits.min_qty

        if min_notional is not None and candidate.notional_estimate < min_notional:
            return RouterVenueScore(
                venue_id=candidate.venue_id,
                score=Decimal("-1_000_000"),
                reason="below_min_notional",
            )

        if min_qty is not None and candidate.quantity < min_qty:
            return RouterVenueScore(
                venue_id=candidate.venue_id,
                score=Decimal("-1_000_000"),
                reason="below_min_qty",
            )

    effective_price = estimate_effective_price(candidate)

    if candidate.side == "buy":
        if effective_price is None:
            return RouterVenueScore(
                venue_id=candidate.venue_id,
                score=Decimal("-1_000_000"),
                reason="no_ask_or_depth",
            )
        price_component = Decimal("1_000_000") / effective_price
    else:
        if effective_price is None:
            return RouterVenueScore(
                venue_id=candidate.venue_id,
                score=Decimal("-1_000_000"),
                reason="no_bid_or_depth",
            )
        price_component = effective_price

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
