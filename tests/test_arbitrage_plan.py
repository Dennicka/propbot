from app.services import arbitrage
import time

from app.services.runtime import get_market_data, get_state, reset_for_tests


def test_build_plan_uses_market_aggregator():
    reset_for_tests()
    state = get_state()
    state.control.min_spread_bps = 0.5
    state.control.taker_fee_bps_binance = 0
    state.control.taker_fee_bps_okx = 0
    state.risk.limits.max_position_usdt["BTCUSDT"] = 1_000.0
    aggregator = get_market_data()
    aggregator.update_from_ws(venue="binance-um", symbol="BTCUSDT", bid=20_000.0, ask=20_001.0)
    aggregator.update_from_ws(venue="okx-perp", symbol="BTCUSDT", bid=20_040.0, ask=20_041.0)
    plan = arbitrage.build_plan("BTCUSDT", 100.0, 0)
    assert plan.symbol == "BTCUSDT"
    assert plan.venues == ["binance-um", "okx-perp"]
    assert plan.spread_bps > 0
    assert plan.viable is True
    assert plan.reason is None


def test_build_plan_blocks_low_spread():
    reset_for_tests()
    state = get_state()
    state.control.min_spread_bps = 5.0
    state.control.taker_fee_bps_binance = 0
    state.control.taker_fee_bps_okx = 0
    aggregator = get_market_data()
    aggregator.update_from_ws(venue="binance-um", symbol="BTCUSDT", bid=20_000.0, ask=20_001.0)
    aggregator.update_from_ws(venue="okx-perp", symbol="BTCUSDT", bid=20_002.0, ask=20_003.0)
    plan = arbitrage.build_plan("BTCUSDT", 100.0, 0)
    assert plan.viable is False
    assert plan.reason is not None


def test_build_plan_includes_funding_when_enabled(monkeypatch):
    reset_for_tests()
    state = get_state()
    state.control.taker_fee_bps_binance = 0
    state.control.taker_fee_bps_okx = 0
    state.control.min_spread_bps = 0.0
    aggregator = get_market_data()
    aggregator.update_from_ws(venue="binance-um", symbol="BTCUSDT", bid=20_010.0, ask=20_011.0)
    aggregator.update_from_ws(venue="okx-perp", symbol="BTCUSDT", bid=20_040.0, ask=20_041.0)
    baseline = arbitrage.build_plan("BTCUSDT", 1_000.0, 0)

    monkeypatch.setenv("FEATURE_FUNDING_ROUTER", "1")
    now = time.time()
    derivatives = state.derivatives
    for venue_id, runtime in derivatives.venues.items():
        client = runtime.client

        monkeypatch.setattr(
            client,
            "get_fees",
            lambda _symbol, _venue=venue_id: {"taker_bps": 0.0},
        )

        base_rate = -0.002 if venue_id == "okx_perp" else 0.0

        monkeypatch.setattr(
            client,
            "get_funding_info",
            lambda _symbol, _rate=base_rate: {"rate": _rate, "next_funding_ts": now + 3600},
        )

    plan_with_funding = arbitrage.build_plan("BTCUSDT", 1_000.0, 0)
    assert plan_with_funding.est_pnl_usdt < baseline.est_pnl_usdt
