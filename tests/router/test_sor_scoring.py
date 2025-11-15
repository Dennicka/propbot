from decimal import Decimal

from app.router.sor_scoring import (
    RouterVenueCandidate,
    RouterVenueCostEstimate,
    RouterVenueMarketSnapshot,
    Side,
    choose_best_venue,
    score_venue_candidate,
)


def _make_candidate(
    venue: str,
    *,
    side: Side,
    bid: Decimal | None,
    ask: Decimal | None,
    fee_rate: Decimal | None = None,
    is_healthy: bool = True,
    risk_allowed: bool = True,
) -> RouterVenueCandidate:
    costs = (
        RouterVenueCostEstimate(fee_rate=fee_rate, funding_rate=None)
        if fee_rate is not None
        else None
    )
    return RouterVenueCandidate(
        venue_id=venue,
        side=side,
        quantity=Decimal("1"),
        notional_estimate=Decimal("100"),
        market=RouterVenueMarketSnapshot(
            venue_id=venue,
            best_bid=bid,
            best_ask=ask,
        ),
        costs=costs,
        is_healthy=is_healthy,
        risk_allowed=risk_allowed,
    )


def test_score_venue_candidate_rejects_unhealthy() -> None:
    candidate = _make_candidate(
        "binance",
        side="buy",
        bid=Decimal("100"),
        ask=Decimal("101"),
        is_healthy=False,
    )

    score = score_venue_candidate(candidate)

    assert score.score < Decimal("0")
    assert score.reason == "unhealthy"


def test_score_venue_candidate_prefers_better_ask_for_buy() -> None:
    good = _make_candidate("binance", side="buy", bid=Decimal("99"), ask=Decimal("100"))
    worse = _make_candidate("okx", side="buy", bid=Decimal("99"), ask=Decimal("110"))

    good_score = score_venue_candidate(good)
    bad_score = score_venue_candidate(worse)

    assert good_score.score > bad_score.score


def test_score_venue_candidate_prefers_better_bid_for_sell() -> None:
    good = _make_candidate("binance", side="sell", bid=Decimal("110"), ask=Decimal("111"))
    worse = _make_candidate("okx", side="sell", bid=Decimal("100"), ask=Decimal("101"))

    good_score = score_venue_candidate(good)
    bad_score = score_venue_candidate(worse)

    assert good_score.score > bad_score.score


def test_choose_best_venue_returns_none_if_all_negative() -> None:
    candidates = [
        _make_candidate("binance", side="buy", bid=None, ask=None, is_healthy=False),
        _make_candidate("okx", side="buy", bid=None, ask=None, risk_allowed=False),
    ]

    best = choose_best_venue(candidates)

    assert best is None


def test_fee_rate_penalises_score() -> None:
    cheap = _make_candidate(
        "binance",
        side="sell",
        bid=Decimal("100"),
        ask=Decimal("101"),
        fee_rate=Decimal("0.0001"),
    )
    expensive = _make_candidate(
        "okx",
        side="sell",
        bid=Decimal("100"),
        ask=Decimal("101"),
        fee_rate=Decimal("0.001"),
    )

    cheap_score = score_venue_candidate(cheap)
    expensive_score = score_venue_candidate(expensive)

    assert cheap_score.score > expensive_score.score
