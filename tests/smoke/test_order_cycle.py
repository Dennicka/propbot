from __future__ import annotations

from decimal import Decimal

import pytest

from app.exchanges.metadata import SymbolMeta, provider
from app.orders.state import OrderState
from app.router.smart_router import SmartRouter


class FakeOrderChannel:
    """Minimal adapter to exercise the smart router order lifecycle."""

    def __init__(self, router: SmartRouter) -> None:
        self._router = router
        self._ts_ns = 1_000_000
        self._nonce = 0

    def submit(
        self,
        *,
        strategy: str,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
    ) -> str:
        self._nonce += 1
        self._ts_ns += 1
        response = self._router.register_order(
            strategy=strategy,
            venue=venue,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            ts_ns=self._ts_ns,
            nonce=self._nonce,
        )
        return str(response["client_order_id"])

    def push(self, client_order_id: str, event: str, quantity: float | None = None) -> OrderState:
        return self._router.process_order_event(
            client_order_id=client_order_id,
            event=event,
            quantity=quantity,
        )


@pytest.fixture
def router() -> SmartRouter:
    provider.clear()
    provider.put(
        "binance",
        "BTCUSDT",
        SymbolMeta(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_notional=Decimal("10"),
            min_qty=Decimal("0.001"),
        ),
    )
    router = SmartRouter()
    yield router
    provider.clear()


def assert_no_anomalies(router: SmartRouter) -> None:
    counters = router.audit_counters_snapshot()
    assert all(value == 0 for value in counters.values()), counters


def test_order_cycle_happy_path(router: SmartRouter) -> None:
    channel = FakeOrderChannel(router)
    order_id = channel.submit(
        strategy="alpha",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=25000.0,
    )

    initial = router.get_order_snapshot(order_id)
    assert initial["state"] == OrderState.PENDING

    state = channel.push(order_id, "ack")
    assert state == OrderState.ACK

    state = channel.push(order_id, "partial_fill", quantity=0.4)
    assert state == OrderState.PARTIAL

    state = channel.push(order_id, "filled")
    assert state == OrderState.FILLED

    final_snapshot = router.get_order_snapshot(order_id)
    assert final_snapshot["state"] == OrderState.FILLED
    assert final_snapshot["filled_qty"] == pytest.approx(final_snapshot["qty"])
    assert_no_anomalies(router)


def test_order_cycle_reject(router: SmartRouter) -> None:
    channel = FakeOrderChannel(router)
    order_id = channel.submit(
        strategy="bravo",
        venue="binance",
        symbol="BTCUSDT",
        side="sell",
        qty=0.5,
        price=26000.0,
    )

    state = channel.push(order_id, "reject")
    assert state == OrderState.REJECTED

    snapshot = router.get_order_snapshot(order_id)
    assert snapshot["state"] == OrderState.REJECTED
    assert snapshot["filled_qty"] == pytest.approx(0.0)
    assert_no_anomalies(router)


def test_order_cycle_cancel(router: SmartRouter) -> None:
    channel = FakeOrderChannel(router)
    order_id = channel.submit(
        strategy="charlie",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=2.0,
        price=25500.0,
    )

    channel.push(order_id, "ack")
    state = channel.push(order_id, "canceled")
    assert state == OrderState.CANCELED

    snapshot = router.get_order_snapshot(order_id)
    assert snapshot["state"] == OrderState.CANCELED
    assert snapshot["filled_qty"] == pytest.approx(0.0)
    assert_no_anomalies(router)


def test_order_cycle_idempotent_events(router: SmartRouter) -> None:
    channel = FakeOrderChannel(router)
    order_id = channel.submit(
        strategy="delta",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=24000.0,
    )

    channel.push(order_id, "ack")
    channel.push(order_id, "partial_fill", quantity=0.3)
    channel.push(order_id, "partial_fill", quantity=0.0)
    channel.push(order_id, "partial_fill", quantity=0.2)

    partial_snapshot = router.get_order_snapshot(order_id)
    assert partial_snapshot["state"] == OrderState.PARTIAL
    assert partial_snapshot["filled_qty"] == pytest.approx(0.5)

    channel.push(order_id, "partial_fill", quantity=0.4)
    channel.push(order_id, "filled")

    final_snapshot = router.get_order_snapshot(order_id)
    assert final_snapshot["state"] == OrderState.FILLED
    assert final_snapshot["filled_qty"] == pytest.approx(final_snapshot["qty"])
    assert_no_anomalies(router)
