from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import ledger
from app.recon.internal_source import InternalStateSource


@pytest.mark.asyncio
async def test_internal_source_loads_internal_state(monkeypatch: pytest.MonkeyPatch) -> None:
    source = InternalStateSource()

    monkeypatch.setattr(
        "app.recon.internal_source.fetch_balances",
        lambda: [
            {"venue": "target", "asset": "btc", "qty": "1.5", "free": "1.0"},
            {"venue": "other", "asset": "eth", "qty": "2"},
        ],
    )
    monkeypatch.setattr(
        "app.recon.internal_source.fetch_positions",
        lambda: [
            {
                "venue": "target",
                "symbol": "BTCUSDT",
                "base_qty": "3",
                "avg_price": "10000",
            },
            {"venue": "other", "symbol": "ETHUSDT", "base_qty": "1", "avg_price": "1200"},
        ],
    )
    monkeypatch.setattr(
        "app.recon.internal_source.fetch_open_orders",
        lambda: [
            {
                "venue": "target",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "qty": "0.5",
                "price": "9000",
                "status": "OPEN",
                "idemp_key": "order-1",
            },
            {
                "venue": "target",
                "symbol": "ETHUSDT",
                "side": "sell",
                "qty": "1.0",
                "price": "1200",
                "status": "PARTIALLY_FILLED",
                "client_order_id": "order-2",
                "exchange_order_id": "ex-2",
            },
            {"venue": "other", "symbol": "LTCUSDT", "side": "BUY", "qty": "1", "price": "80"},
        ],
    )

    balances = await source.load_balances("target")
    positions = await source.load_positions("target")
    orders = await source.load_open_orders("target")

    assert len(balances) == 1
    assert balances[0].asset == "BTC"
    assert balances[0].total == Decimal("1.5")
    assert balances[0].available == Decimal("1.0")

    assert len(positions) == 1
    assert positions[0].symbol == "BTCUSDT"
    assert positions[0].qty == Decimal("3")
    assert positions[0].entry_price == Decimal("10000")
    assert positions[0].notional == Decimal("30000")

    assert len(orders) == 2
    first_order = orders[0]
    assert first_order.side == "buy"
    assert first_order.qty == Decimal("0.5")
    assert first_order.price == Decimal("9000")
    assert first_order.client_order_id == "order-1"

    second_order = orders[1]
    assert second_order.side == "sell"
    assert second_order.client_order_id == "order-2"
    assert second_order.exchange_order_id == "ex-2"


@pytest.mark.asyncio
async def test_internal_source_reads_from_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    ledger_path = tmp_path / "ledger.db"
    monkeypatch.setattr(ledger, "LEDGER_PATH", ledger_path)
    ledger.init_db()
    ledger.reset()

    ts = datetime.now(timezone.utc).isoformat()
    filled_order_id = ledger.record_order(
        venue="binance",  # internal ledger venue name
        symbol="BTCUSDT",
        side="buy",
        qty=1.5,
        price=20_000.0,
        status="filled",
        client_ts=ts,
        exchange_ts=ts,
        idemp_key="filled-order",
    )
    ledger.record_fill(
        order_id=filled_order_id,
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.5,
        price=20_000.0,
        fee=15.0,
        ts=ts,
    )
    ledger.update_order_status(filled_order_id, "filled")

    ledger.record_order(
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=0.25,
        price=21_000.0,
        status="open",
        client_ts=ts,
        exchange_ts=None,
        idemp_key="open-order",
    )

    source = InternalStateSource()

    balances = await source.load_balances("binance")
    positions = await source.load_positions("binance")
    orders = await source.load_open_orders("binance")

    assert len(balances) == 1
    balance = balances[0]
    assert balance.asset == "USDT"
    assert balance.total == Decimal("-30015")
    assert balance.available == Decimal("-30015")

    assert len(positions) == 1
    position = positions[0]
    assert position.symbol == "BTCUSDT"
    assert position.qty == Decimal("1.5")
    assert position.entry_price == Decimal("20000")
    assert position.notional == Decimal("30000")

    assert len(orders) == 1
    order = orders[0]
    assert order.client_order_id == "open-order"
    assert order.qty == Decimal("0.25")
    assert order.price == Decimal("21000")
    assert order.side == "buy"

    ledger.reset()
