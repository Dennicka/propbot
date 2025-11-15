from decimal import Decimal

from app.router.sor_scoring import (
    RouterVenueCandidate,
    RouterVenueCostEstimate,
    RouterVenueMarketSnapshot,
    RouterVenueTradingLimits,
    Side,
    choose_best_venue,
    estimate_effective_price,
    score_venue_candidate,
)


def _make_candidate(
    venue: str,
    *,
    side: Side,
    bid: Decimal | None,
    ask: Decimal | None,
    bid_qty: Decimal | None = None,
    ask_qty: Decimal | None = None,
    fee_rate: Decimal | None = None,
    is_healthy: bool = True,
    risk_allowed: bool = True,
    quantity: Decimal = Decimal("1"),
    notional: Decimal = Decimal("100"),
    limits: RouterVenueTradingLimits | None = None,
) -> RouterVenueCandidate:
    costs = (
        RouterVenueCostEstimate(fee_rate=fee_rate, funding_rate=None)
        if fee_rate is not None
        else None
    )
    return RouterVenueCandidate(
        venue_id=venue,
        side=side,
        quantity=quantity,
        notional_estimate=notional,
        market=RouterVenueMarketSnapshot(
            venue_id=venue,
            best_bid=bid,
            best_ask=ask,
            best_bid_qty=bid_qty,
            best_ask_qty=ask_qty,
        ),
        costs=costs,
        is_healthy=is_healthy,
        risk_allowed=risk_allowed,
        limits=limits,
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
    good = _make_candidate(
        "binance",
        side="buy",
        bid=Decimal("99"),
        ask=Decimal("100"),
        ask_qty=Decimal("10"),
    )
    worse = _make_candidate(
        "okx",
        side="buy",
        bid=Decimal("99"),
        ask=Decimal("110"),
        ask_qty=Decimal("10"),
    )

    good_score = score_venue_candidate(good)
    bad_score = score_venue_candidate(worse)

    assert good_score.score > bad_score.score


def test_score_venue_candidate_prefers_better_bid_for_sell() -> None:
    good = _make_candidate(
        "binance",
        side="sell",
        bid=Decimal("110"),
        ask=Decimal("111"),
        bid_qty=Decimal("10"),
    )
    worse = _make_candidate(
        "okx",
        side="sell",
        bid=Decimal("100"),
        ask=Decimal("101"),
        bid_qty=Decimal("10"),
    )

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
        bid_qty=Decimal("10"),
        fee_rate=Decimal("0.0001"),
    )
    expensive = _make_candidate(
        "okx",
        side="sell",
        bid=Decimal("100"),
        ask=Decimal("101"),
        bid_qty=Decimal("10"),
        fee_rate=Decimal("0.001"),
    )

    cheap_score = score_venue_candidate(cheap)
    expensive_score = score_venue_candidate(expensive)

    assert cheap_score.score > expensive_score.score


def test_estimate_effective_price_no_depth_returns_none() -> None:
    candidate = _make_candidate(
        "binance",
        side="buy",
        bid=Decimal("99"),
        ask=Decimal("100"),
        ask_qty=None,
    )

    assert estimate_effective_price(candidate) is None


def test_estimate_effective_price_buy_small_vs_depth() -> None:
    candidate = _make_candidate(
        "binance",
        side="buy",
        bid=Decimal("99"),
        ask=Decimal("100"),
        ask_qty=Decimal("10"),
        quantity=Decimal("5"),
    )

    assert estimate_effective_price(candidate) == Decimal("100")


def test_estimate_effective_price_buy_large_vs_depth() -> None:
    candidate = _make_candidate(
        "binance",
        side="buy",
        bid=Decimal("99"),
        ask=Decimal("100"),
        ask_qty=Decimal("10"),
        quantity=Decimal("20"),
    )

    effective_price = estimate_effective_price(candidate)

    assert effective_price is not None
    assert effective_price > Decimal("100")


def test_score_venue_candidate_rejects_below_min_notional() -> None:
    candidate = _make_candidate(
        "binance",
        side="buy",
        bid=Decimal("99"),
        ask=Decimal("100"),
        ask_qty=Decimal("10"),
        notional=Decimal("50"),
        limits=RouterVenueTradingLimits(min_notional=Decimal("100")),
    )

    score = score_venue_candidate(candidate)

    assert score.score < Decimal("0")
    assert score.reason == "below_min_notional"


def test_score_venue_candidate_prefers_venue_with_better_effective_price() -> None:
    good = _make_candidate(
        "binance",
        side="buy",
        bid=Decimal("99"),
        ask=Decimal("100"),
        ask_qty=Decimal("100"),
        quantity=Decimal("10"),
    )
    worse = _make_candidate(
        "okx",
        side="buy",
        bid=Decimal("99"),
        ask=Decimal("100"),
        ask_qty=Decimal("5"),
        quantity=Decimal("10"),
    )

    good_score = score_venue_candidate(good)
    bad_score = score_venue_candidate(worse)

    assert good_score.score > bad_score.score
