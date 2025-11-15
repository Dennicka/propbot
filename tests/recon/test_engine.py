from __future__ import annotations

from decimal import Decimal

import pytest

from app.recon.engine import (
    build_recon_snapshot,
    reconcile_balances,
    reconcile_orders,
    reconcile_positions,
)
from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
)


@pytest.fixture()
def venue_id() -> str:
    return "test-venue"


def test_reconcile_balances_match(venue_id: str) -> None:
    internal = [
        ExchangeBalanceSnapshot(
            venue_id=venue_id,
            asset="USDT",
            total=Decimal("100"),
            available=Decimal("80"),
        )
    ]
    external = [
        ExchangeBalanceSnapshot(
            venue_id=venue_id,
            asset="USDT",
            total=Decimal("100"),
            available=Decimal("75"),
        )
    ]

    issues = reconcile_balances(venue_id, internal, external)
    assert issues == []


def test_reconcile_balances_detects_mismatch(venue_id: str) -> None:
    internal = [
        ExchangeBalanceSnapshot(
            venue_id=venue_id,
            asset="USDT",
            total=Decimal("100"),
            available=Decimal("80"),
        )
    ]
    external = [
        ExchangeBalanceSnapshot(
            venue_id=venue_id,
            asset="USDT",
            total=Decimal("99.5"),
            available=Decimal("75"),
        )
    ]

    issues = reconcile_balances(venue_id, internal, external, tolerance=Decimal("0.1"))
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "balance_mismatch"
    assert issue.severity == "error"


def test_reconcile_balances_missing_side(venue_id: str) -> None:
    internal = []
    external = [
        ExchangeBalanceSnapshot(
            venue_id=venue_id,
            asset="BTC",
            total=Decimal("1"),
            available=Decimal("0.5"),
        )
    ]

    issues = reconcile_balances(venue_id, internal, external)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "missing_internal"
    assert issue.asset == "BTC"


def test_reconcile_positions_detects_mismatch(venue_id: str) -> None:
    internal = [
        ExchangePositionSnapshot(
            venue_id=venue_id,
            symbol="ETHUSDT",
            qty=Decimal("1"),
            entry_price=Decimal("1800"),
            notional=Decimal("1800"),
        )
    ]
    external = [
        ExchangePositionSnapshot(
            venue_id=venue_id,
            symbol="ETHUSDT",
            qty=Decimal("0.8"),
            entry_price=Decimal("1790"),
            notional=Decimal("1432"),
        )
    ]

    issues = reconcile_positions(
        venue_id,
        internal,
        external,
        qty_tolerance=Decimal("0.01"),
        notional_tolerance=Decimal("10"),
    )
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "position_mismatch"
    assert issue.severity == "error"


def test_reconcile_positions_missing_internal(venue_id: str) -> None:
    internal: list[ExchangePositionSnapshot] = []
    external = [
        ExchangePositionSnapshot(
            venue_id=venue_id,
            symbol="BTCUSDT",
            qty=Decimal("0.5"),
            entry_price=Decimal("25000"),
            notional=Decimal("12500"),
        )
    ]

    issues = reconcile_positions(venue_id, internal, external)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "missing_internal"
    assert issue.symbol == "BTCUSDT"


def test_reconcile_orders_detects_mismatch(venue_id: str) -> None:
    internal = [
        ExchangeOrderSnapshot(
            venue_id=venue_id,
            symbol="ETHUSDT",
            client_order_id="order-1",
            exchange_order_id=None,
            side="buy",
            qty=Decimal("1"),
            price=Decimal("1800"),
            status="open",
        )
    ]
    external = [
        ExchangeOrderSnapshot(
            venue_id=venue_id,
            symbol="ETHUSDT",
            client_order_id="order-1",
            exchange_order_id=None,
            side="sell",
            qty=Decimal("1"),
            price=Decimal("1805"),
            status="open",
        )
    ]

    issues = reconcile_orders(venue_id, internal, external)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "order_mismatch"
    assert issue.severity == "error"


def test_reconcile_orders_missing_external(venue_id: str) -> None:
    internal = [
        ExchangeOrderSnapshot(
            venue_id=venue_id,
            symbol="BTCUSDT",
            client_order_id=None,
            exchange_order_id="ex-1",
            side="buy",
            qty=Decimal("0.5"),
            price=Decimal("25000"),
            status="open",
        )
    ]
    external: list[ExchangeOrderSnapshot] = []

    issues = reconcile_orders(venue_id, internal, external)
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "missing_external"
    assert issue.symbol == "BTCUSDT"


def test_build_recon_snapshot_aggregates_all(venue_id: str) -> None:
    balances_internal = [
        ExchangeBalanceSnapshot(
            venue_id=venue_id,
            asset="USDT",
            total=Decimal("100"),
            available=Decimal("90"),
        )
    ]
    balances_external = [
        ExchangeBalanceSnapshot(
            venue_id=venue_id,
            asset="USDT",
            total=Decimal("99"),
            available=Decimal("90"),
        )
    ]

    positions_internal = [
        ExchangePositionSnapshot(
            venue_id=venue_id,
            symbol="BTCUSDT",
            qty=Decimal("1"),
            entry_price=Decimal("20000"),
            notional=Decimal("20000"),
        )
    ]
    positions_external: list[ExchangePositionSnapshot] = []

    orders_internal = [
        ExchangeOrderSnapshot(
            venue_id=venue_id,
            symbol="ETHUSDT",
            client_order_id="order-1",
            exchange_order_id=None,
            side="buy",
            qty=Decimal("1"),
            price=Decimal("1800"),
            status="open",
        )
    ]
    orders_external = [
        ExchangeOrderSnapshot(
            venue_id=venue_id,
            symbol="ETHUSDT",
            client_order_id="order-1",
            exchange_order_id=None,
            side="buy",
            qty=Decimal("1"),
            price=Decimal("1810"),
            status="open",
        )
    ]

    snapshot = build_recon_snapshot(
        venue_id,
        balances_internal=balances_internal,
        balances_external=balances_external,
        positions_internal=positions_internal,
        positions_external=positions_external,
        orders_internal=orders_internal,
        orders_external=orders_external,
    )

    kinds = {issue.kind for issue in snapshot.issues}
    assert kinds == {"balance_mismatch", "missing_external", "order_mismatch"}
    assert snapshot.venue_id == venue_id
