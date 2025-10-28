from app.services import arbitrage
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
