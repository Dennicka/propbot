from __future__ import annotations

from decimal import Decimal

import pytest

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
