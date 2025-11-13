"""Tests for SmartRouter audit counters and lifecycle guards."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.orders.state import OrderState
from app.router.smart_router import SmartRouter


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> SmartRouter:
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    state = SimpleNamespace(
        config=SimpleNamespace(data=None), control=SimpleNamespace(post_only=False)
    )
    return SmartRouter(state=state, market_data=SimpleNamespace())


def _register(router: SmartRouter) -> str:
    payload = router.register_order(
        strategy="alpha",
        venue="test-venue",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts_ns=1,
        nonce=1,
    )
    return str(payload["client_order_id"])


def test_duplicate_event_increment(router: SmartRouter) -> None:
    coid = _register(router)
    first_state = router.process_order_event(client_order_id=coid, event="ack")
    assert first_state == OrderState.ACK

    duplicate_state = router.process_order_event(client_order_id=coid, event="ack")
    assert duplicate_state == OrderState.ACK

    snapshot = router.audit_counters_snapshot()
    assert snapshot["duplicate_event"] == 1
    tracked = router._order_tracker.get(coid)
    assert tracked is not None and tracked.state == OrderState.ACK


def test_out_of_order_partial_fill(router: SmartRouter) -> None:
    coid = _register(router)

    state = router.process_order_event(client_order_id=coid, event="partial_fill", quantity=0.5)
    assert state == OrderState.PENDING

    snapshot = router.audit_counters_snapshot()
    assert snapshot["out_of_order"] == 1
    tracked = router._order_tracker.get(coid)
    assert tracked is not None and tracked.state == OrderState.PENDING


def test_fill_without_ack(router: SmartRouter) -> None:
    coid = _register(router)

    state = router.process_order_event(client_order_id=coid, event="filled", quantity=1.0)
    assert state == OrderState.PENDING

    snapshot = router.audit_counters_snapshot()
    assert snapshot["fill_without_ack"] == 1
    tracked = router._order_tracker.get(coid)
    assert tracked is not None and tracked.state == OrderState.PENDING


def test_ack_missing_register(router: SmartRouter) -> None:
    with pytest.raises(KeyError):
        router.process_order_event(client_order_id="missing", event="ack")

    snapshot = router.audit_counters_snapshot()
    assert snapshot["ack_missing_register"] == 1
