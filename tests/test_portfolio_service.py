from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import ledger
from app.services import portfolio
from app.services.runtime import get_state


@pytest.mark.asyncio
async def test_portfolio_snapshot_from_ledger():
    ledger.reset()
    state = get_state()
    state.control.environment = "paper"
    state.control.safe_mode = True
    state.control.dry_run = True

    ts = datetime.now(timezone.utc).isoformat()
    buy_order_1 = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        status="filled",
        client_ts=ts,
        exchange_ts=ts,
        idemp_key="agg-ledger-buy-1",
    )
    ledger.record_fill(
        order_id=buy_order_1,
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        fee=0.0,
        ts=ts,
    )
    buy_order_2 = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        status="filled",
        client_ts=ts,
        exchange_ts=ts,
        idemp_key="agg-ledger-buy-2",
    )
    ledger.record_fill(
        order_id=buy_order_2,
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        fee=0.0,
        ts=ts,
    )
    sell_order = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="sell",
        qty=1.0,
        price=120.0,
        status="filled",
        client_ts=ts,
        exchange_ts=ts,
        idemp_key="agg-ledger-sell",
    )
    ledger.record_fill(
        order_id=sell_order,
        venue="binance-um",
        symbol="BTCUSDT",
        side="sell",
        qty=1.0,
        price=120.0,
        fee=0.0,
        ts=ts,
    )

    snapshot = await portfolio.snapshot()
    assert snapshot.positions
    position = snapshot.positions[0]
    assert position.symbol == "BTCUSDT"
    assert position.qty == pytest.approx(1.0)
    assert position.entry_px == pytest.approx(100.0)
    assert position.mark_px == pytest.approx(100.0)
    assert position.upnl == pytest.approx(0.0)
    assert position.rpnl == pytest.approx(20.0)
    assert position.fees_paid == pytest.approx(0.0)
    assert position.funding == pytest.approx(0.0)
    assert snapshot.notional_total == pytest.approx(abs(position.qty) * position.mark_px)
    assert snapshot.pnl_totals["realized"] == pytest.approx(20.0)
    assert snapshot.pnl_totals["realized_trading"] == pytest.approx(20.0)
    assert snapshot.pnl_totals["unrealized"] == pytest.approx(0.0)
    assert snapshot.pnl_totals["total"] == pytest.approx(20.0)
    assert snapshot.pnl_totals["fees"] == pytest.approx(0.0)
    assert snapshot.pnl_totals["funding"] == pytest.approx(0.0)
    assert snapshot.pnl_totals["net"] == pytest.approx(20.0)
    assert snapshot.profile == "paper"
    assert snapshot.exclude_simulated is True
    assert snapshot.balances
    assert any(balance.asset == "USDT" for balance in snapshot.balances)
