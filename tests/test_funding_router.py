import time

import pytest

from app.routing.funding_router import VenueQuote, choose_best_pair, compute_effective_cost


def test_compute_effective_cost_handles_long_short():
    long_cost = compute_effective_cost(
        taker_fee_bps=5.0, funding_rate=0.0001, horizon=1.0, side="long"
    )
    short_cost = compute_effective_cost(
        taker_fee_bps=5.0, funding_rate=0.0001, horizon=1.0, side="short"
    )
    assert long_cost == pytest.approx(6.0)
    assert short_cost == pytest.approx(4.0)


def test_choose_best_pair_prefers_lower_total_cost():
    now = time.time()
    quotes = {
        "binance": VenueQuote(
            venue="binance",
            taker_fee_bps=2.0,
            maker_fee_bps=1.0,
            vip_rebate_bps=0.0,
            maker_possible=False,
            funding_rate=0.001,
            next_funding_ts=now + 1800,
        ),
        "okx": VenueQuote(
            venue="okx",
            taker_fee_bps=2.0,
            maker_fee_bps=1.5,
            vip_rebate_bps=0.0,
            maker_possible=False,
            funding_rate=-0.002,
            next_funding_ts=now + 1800,
        ),
    }
    best = choose_best_pair(quotes, include_next_window=True, now=now)
    assert best is not None
    assert best.long_venue == "okx"
    assert best.short_venue == "binance"


def test_choose_best_pair_uses_tca_router(monkeypatch):
    monkeypatch.setenv("FEATURE_TCA_ROUTER", "1")
    now = time.time()
    horizon = now + 3600
    quotes = {
        "maker": VenueQuote(
            venue="maker",
            taker_fee_bps=4.0,
            maker_fee_bps=1.0,
            vip_rebate_bps=0.7,
            maker_possible=True,
            funding_rate=0.001,
            next_funding_ts=horizon,
        ),
        "other": VenueQuote(
            venue="other",
            taker_fee_bps=3.0,
            maker_fee_bps=3.0,
            vip_rebate_bps=0.0,
            maker_possible=True,
            funding_rate=0.0,
            next_funding_ts=horizon,
        ),
    }
    best = choose_best_pair(quotes, include_next_window=True, now=now)
    assert best is not None
    assert best.long_venue == "other"
    assert best.short_venue == "maker"
    monkeypatch.delenv("FEATURE_TCA_ROUTER", raising=False)
