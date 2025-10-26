import math

from positions import close_position, create_position, list_positions, reset_positions
from services.risk_manager import can_open_new_position


def setup_function(_):
    reset_positions()


def test_close_position_updates_status_and_pnl():
    position = create_position(
        symbol="ETHUSDT",
        long_venue="binance",
        short_venue="okx",
        notional_usdt=1000.0,
        entry_spread_bps=25.0,
        leverage=3.0,
        entry_long_price=100.0,
        entry_short_price=101.0,
    )
    closed = close_position(
        position["id"], exit_long_price=102.0, exit_short_price=104.0
    )
    assert closed["status"] == "closed"
    assert math.isclose(closed["pnl_usdt"], (104.0 - 102.0) * (1000.0 / 100.0))
    assert "closed_ts" in closed
    assert list_positions()[0]["status"] == "closed"


def test_risk_manager_blocks_excess_notional(monkeypatch):
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")
    monkeypatch.setenv("MAX_NOTIONAL_PER_POSITION_USDT", "2000")
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "1500")
    create_position(
        symbol="BTCUSDT",
        long_venue="binance",
        short_venue="okx",
        notional_usdt=900.0,
        entry_spread_bps=15.0,
        leverage=2.0,
        entry_long_price=30_000.0,
        entry_short_price=30_005.0,
    )
    allowed, reason = can_open_new_position(800.0, 2.0)
    assert allowed is False
    assert reason == "total_notional_limit_exceeded"
