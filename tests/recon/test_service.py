from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app import ledger
from app.recon.internal_source import InternalStateSource
from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
)
from app.recon.service import ReconService


class _StubExternalStateSource:
    def __init__(
        self,
        *,
        balances: list[ExchangeBalanceSnapshot],
        positions: list[ExchangePositionSnapshot],
        orders: list[ExchangeOrderSnapshot],
    ) -> None:
        self._balances = balances
        self._positions = positions
        self._orders = orders

    async def load_balances(self, venue_id: str):  # pragma: no cover - trivial passthrough
        return list(self._balances if venue_id == "binance" else [])

    async def load_positions(self, venue_id: str):  # pragma: no cover - trivial passthrough
        return list(self._positions if venue_id == "binance" else [])

    async def load_open_orders(self, venue_id: str):  # pragma: no cover - trivial passthrough
        return list(self._orders if venue_id == "binance" else [])


@pytest.mark.asyncio
async def test_recon_service_uses_internal_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    ledger_path = tmp_path / "ledger.db"
    monkeypatch.setattr(ledger, "LEDGER_PATH", ledger_path)
    ledger.init_db()
    ledger.reset()

    ts = datetime.now(timezone.utc).isoformat()
    filled_order_id = ledger.record_order(
        venue="binance",
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

    expected_balance = ExchangeBalanceSnapshot(
        venue_id="binance",
        asset="USDT",
        total=Decimal("-30015"),
        available=Decimal("-30015"),
    )
    expected_position = ExchangePositionSnapshot(
        venue_id="binance",
        symbol="BTCUSDT",
        qty=Decimal("1.5"),
        entry_price=Decimal("20000"),
        notional=Decimal("30000"),
    )
    expected_order = ExchangeOrderSnapshot(
        venue_id="binance",
        symbol="BTCUSDT",
        client_order_id="open-order",
        exchange_order_id=None,
        side="buy",
        qty=Decimal("0.25"),
        price=Decimal("21000"),
        status="open",
    )

    stub_external = _StubExternalStateSource(
        balances=[expected_balance],
        positions=[expected_position],
        orders=[expected_order],
    )

    monkeypatch.setattr("app.recon.service.emit_recon_alerts", lambda snapshot: None)

    service = ReconService(
        internal_source=InternalStateSource(),
        external_source=stub_external,
    )

    snapshot = await service.run_for_venue("binance")

    assert snapshot.balances_internal == [expected_balance]
    assert snapshot.positions_internal == [expected_position]
    assert snapshot.open_orders_internal == [expected_order]

    ledger.reset()
