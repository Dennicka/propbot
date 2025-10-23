from __future__ import annotations

import pytest

from app.services.pnl import Fill, Position, compute_realized_pnl, compute_unrealized_pnl


def test_compute_realized_pnl_handles_long_and_short():
    fills = [
        Fill(symbol="BTCUSDT", qty=1.0, price=100.0, side="buy", fee=0.05),
        Fill(symbol="BTCUSDT", qty=1.0, price=110.0, side="sell", fee=0.05),
    ]
    realized = compute_realized_pnl(fills)
    assert realized == pytest.approx((110.0 - 100.0) * 1.0 - 0.1)

    short_fills = [
        Fill(symbol="ETHUSDT", qty=2.0, price=200.0, side="sell", fee=0.0),
        Fill(symbol="ETHUSDT", qty=1.0, price=180.0, side="buy", fee=0.0),
    ]
    realized_short = compute_realized_pnl(short_fills)
    assert realized_short == pytest.approx((200.0 - 180.0) * 1.0)


def test_compute_unrealized_pnl_supports_signed_positions():
    positions = [
        Position(symbol="BTCUSDT", qty=1.0, avg_entry=100.0),
        Position(symbol="ETHUSDT", qty=-2.0, avg_entry=200.0),
    ]
    marks = {"BTCUSDT": 105.0, "ETHUSDT": 190.0}
    unrealized = compute_unrealized_pnl(positions, marks)
    expected = (105.0 - 100.0) * 1.0 + (190.0 - 200.0) * -2.0
    assert unrealized == pytest.approx(expected)
