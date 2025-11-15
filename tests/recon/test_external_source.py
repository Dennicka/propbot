from __future__ import annotations

from decimal import Decimal
from typing import Sequence

import pytest

from app.recon.external_source import ExternalStateSource
from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
)


class FakeExchangeClient:
    def __init__(
        self,
        *,
        balances: Sequence[ExchangeBalanceSnapshot] = (),
        positions: Sequence[ExchangePositionSnapshot] = (),
        orders: Sequence[ExchangeOrderSnapshot] = (),
    ) -> None:
        self._balances = list(balances)
        self._positions = list(positions)
        self._orders = list(orders)

    async def load_balances(self, venue_id: str) -> Sequence[ExchangeBalanceSnapshot]:
        return list(self._balances)

    async def load_positions(self, venue_id: str) -> Sequence[ExchangePositionSnapshot]:
        return list(self._positions)

    async def load_open_orders(self, venue_id: str) -> Sequence[ExchangeOrderSnapshot]:
        return list(self._orders)


@pytest.mark.asyncio
async def test_external_source_uses_injected_client() -> None:
    balance = ExchangeBalanceSnapshot(
        venue_id="binance_um",
        asset="USDT",
        total=Decimal("100"),
        available=Decimal("50"),
    )
    position = ExchangePositionSnapshot(
        venue_id="binance_um",
        symbol="BTCUSDT",
        qty=Decimal("1"),
        entry_price=Decimal("25000"),
        notional=Decimal("25000"),
    )
    order = ExchangeOrderSnapshot(
        venue_id="binance_um",
        symbol="BTCUSDT",
        client_order_id="abc",
        exchange_order_id="def",
        side="buy",
        qty=Decimal("0.5"),
        price=Decimal("20000"),
        status="open",
    )
    fake = FakeExchangeClient(balances=[balance], positions=[position], orders=[order])
    source = ExternalStateSource(clients={"binance_um": fake})

    balances = await source.load_balances("binance_um")
    positions = await source.load_positions("binance_um")
    orders = await source.load_open_orders("binance_um")

    assert balances == [balance]
    assert positions == [position]
    assert orders == [order]


@pytest.mark.asyncio
async def test_external_source_unknown_venue_returns_empty() -> None:
    source = ExternalStateSource(clients={})
    balances = await source.load_balances("unknown")
    positions = await source.load_positions("unknown")
    orders = await source.load_open_orders("unknown")

    assert balances == []
    assert positions == []
    assert orders == []
