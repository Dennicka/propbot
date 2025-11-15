"""In-memory decision log for the smart order router."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Deque, Sequence

from app.router.sor_scoring import (
    RouterVenueCandidate,
    RouterVenueScore,
    Side,
    VenueId,
    score_venue_candidate,
)

_MAX_ENTRIES = 500


@dataclass(slots=True)
class RouterDecisionLogCandidateView:
    """Compact view of a SOR candidate for logging/debug."""

    venue_id: VenueId
    side: Side
    quantity: Decimal
    notional_estimate: Decimal
    is_healthy: bool
    risk_allowed: bool
    price_bid: Decimal | None
    price_ask: Decimal | None
    score: Decimal
    score_reason: str | None


@dataclass(slots=True)
class RouterDecisionLogEntry:
    """Single SOR decision log entry."""

    ts: datetime
    symbol: str
    strategy_id: str | None
    runtime_profile: str

    candidates: Sequence[RouterDecisionLogCandidateView]
    chosen_venue_id: VenueId | None
    chosen_score: Decimal | None
    reject_reason: str | None


_decisions: Deque[RouterDecisionLogEntry] = deque(maxlen=_MAX_ENTRIES)


def append_router_decision(entry: RouterDecisionLogEntry) -> None:
    """Append a SOR decision to the in-memory ring buffer."""

    _decisions.append(entry)


def get_recent_router_decisions(limit: int = 100) -> list[RouterDecisionLogEntry]:
    """Return last ``limit`` SOR decisions (most recent first)."""

    if limit <= 0:
        return []
    items = list(_decisions)
    items.reverse()
    return items[:limit]


def build_candidate_view(
    candidate: RouterVenueCandidate, score: RouterVenueScore
) -> RouterDecisionLogCandidateView:
    """Return a :class:`RouterDecisionLogCandidateView` for ``candidate``."""

    market = candidate.market
    return RouterDecisionLogCandidateView(
        venue_id=candidate.venue_id,
        side=candidate.side,
        quantity=candidate.quantity,
        notional_estimate=candidate.notional_estimate,
        is_healthy=candidate.is_healthy,
        risk_allowed=candidate.risk_allowed,
        price_bid=market.best_bid,
        price_ask=market.best_ask,
        score=score.score,
        score_reason=score.reason,
    )


def make_log_entry(
    *,
    symbol: str,
    strategy_id: str | None,
    runtime_profile: str,
    candidates: Sequence[RouterVenueCandidate],
    chosen: RouterVenueScore | None,
    reject_reason: str | None = None,
) -> RouterDecisionLogEntry:
    """Create a :class:`RouterDecisionLogEntry` from router inputs."""

    candidate_views = [
        build_candidate_view(candidate, score_venue_candidate(candidate))
        for candidate in candidates
    ]
    chosen_venue_id = None
    chosen_score = None
    if chosen is not None:
        chosen_venue_id = chosen.venue_id
        chosen_score = chosen.score
        reject_reason = None
    else:
        reject_reason = reject_reason or "no_venue_selected"

    return RouterDecisionLogEntry(
        ts=datetime.now(timezone.utc),
        symbol=symbol,
        strategy_id=strategy_id,
        runtime_profile=runtime_profile,
        candidates=candidate_views,
        chosen_venue_id=chosen_venue_id,
        chosen_score=chosen_score,
        reject_reason=reject_reason,
    )


def reset_router_decisions_for_tests() -> None:
    """Clear the in-memory log. Intended for use in tests."""

    _decisions.clear()


__all__ = [
    "RouterDecisionLogCandidateView",
    "RouterDecisionLogEntry",
    "append_router_decision",
    "get_recent_router_decisions",
    "make_log_entry",
    "reset_router_decisions_for_tests",
]
