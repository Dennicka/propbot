from __future__ import annotations

import logging
import time
import types
from decimal import Decimal
from typing import Callable

import pytest

from app.orders.state import OrderState
from app.orders.tracker import (
    OrderTracker,
    reset_tracker_metrics,
    tracker_metrics_snapshot,
    tracker_ttl_seconds,
)
from app.router.smart_router import SmartRouter


def _register_order(tracker: OrderTracker, coid: str, now_ns: int) -> None:
    tracker.register_order(
        coid,
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=Decimal("5"),
        now_ns=now_ns,
    )
    tracker.apply_event(coid, "submit", None, now_ns + 1)


def _finalize(
    tracker: OrderTracker, coid: str, now_ns: int, sequence: tuple[str, ...]
) -> OrderState:
    state = OrderState.NEW
    for offset, event in enumerate(sequence, start=2):
        qty = Decimal("1") if event == "partial_fill" else None
        state = tracker.apply_event(coid, event, qty, now_ns + offset)
    assert tracker.get(coid) is not None
    tracker.finalize(coid, state)
    return state


def test_completed_orders_are_removed() -> None:
    reset_tracker_metrics()
    tracker = OrderTracker()
    now = 1_000
    scenarios = {
        "filled": ("ack", "partial_fill", "filled"),
        "canceled": ("ack", "canceled"),
        "rejected": ("reject",),
        "expired": ("ack", "expire"),
    }

    for idx, sequence in enumerate(scenarios.values()):
        coid = f"order-{idx}"
        _register_order(tracker, coid, now + idx * 10)
        final_state = _finalize(tracker, coid, now + idx * 10, sequence)
        assert OrderTracker.is_terminal(final_state)
        assert tracker.get(coid) is None

    assert len(tracker) == 0
    snapshot = tracker_metrics_snapshot()
    assert snapshot["tracked"] == 0
    assert sum(snapshot["finalized"].values()) == len(scenarios)


def test_register_order_is_idempotent(caplog: pytest.LogCaptureFixture) -> None:
    reset_tracker_metrics()
    tracker = OrderTracker()
    now = 2_000
    coid = "duplicate-order"
    _register_order(tracker, coid, now)

    with caplog.at_level(logging.WARNING):
        tracker.register_order(
            coid,
            venue="kraken",
            symbol="ETHUSDT",
            side="sell",
            qty=Decimal("10"),
            now_ns=now + 5,
        )

    tracked = tracker.get(coid)
    assert tracked is not None
    assert tracked.venue == "binance"
    assert tracked.symbol == "BTCUSDT"
    assert len(tracker) == 1
    assert any(
        "order_tracker.duplicate_registration" in record.message for record in caplog.records
    )


def test_metrics_reflect_finalization_counts() -> None:
    reset_tracker_metrics()
    tracker = OrderTracker()
    now = 3_000
    active = 3
    finalized = (
        ("done-0", ("ack", "filled")),
        ("done-1", ("ack", "canceled")),
        ("done-2", ("ack", "expire")),
        ("done-3", ("reject",)),
    )

    for idx in range(active):
        coid = f"active-{idx}"
        _register_order(tracker, coid, now + idx * 20)
        tracker.apply_event(coid, "ack", None, now + idx * 20 + 2)

    for idx, sequence in enumerate(finalized):
        coid, events = sequence
        base_ns = now + (idx + active) * 20
        _register_order(tracker, coid, base_ns)
        final_state = _finalize(tracker, coid, base_ns, events)
        assert OrderTracker.is_terminal(final_state)

    snapshot = tracker_metrics_snapshot()
    assert len(tracker) == active
    assert snapshot["tracked"] == active
    assert sum(snapshot["finalized"].values()) == len(finalized)
    assert {
        "filled",
        "canceled",
        "expired",
        "rejected",
    } == set(snapshot["finalized"].keys())


def _register_router_order(
    router: SmartRouter,
    *,
    ts_ns: int | None = None,
    nonce: int = 1,
) -> str:
    issued_ns = ts_ns if ts_ns is not None else time.time_ns()
    response = router.register_order(
        strategy="test",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=None,
        ts_ns=issued_ns,
        nonce=nonce,
    )
    return str(response["client_order_id"])


@pytest.fixture
def router_factory(monkeypatch: pytest.MonkeyPatch) -> Callable[[], SmartRouter]:
    monkeypatch.setattr("app.router.smart_router.ff.pretrade_strict_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.md_watchdog_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.risk_limits_on", lambda: False)
    monkeypatch.setattr(
        "app.router.smart_router.SafeMode.is_active",
        classmethod(lambda cls: False),
    )
    monkeypatch.setattr(
        "app.router.smart_router.provider.get",
        lambda *_: types.SimpleNamespace(
            tick_size=Decimal("0.1"),
            step_size=Decimal("0.001"),
            min_notional=None,
            min_qty=None,
        ),
    )
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    monkeypatch.setattr(
        "app.router.smart_router.get_profile",
        lambda: types.SimpleNamespace(name="pytest"),
    )
    monkeypatch.setattr("app.router.smart_router.is_live", lambda *_: False)

    def factory() -> SmartRouter:
        state = types.SimpleNamespace(config=None)
        market = types.SimpleNamespace()
        return SmartRouter(state=state, market_data=market)

    return factory


def test_remove_on_terminal_state(router_factory: Callable[[], SmartRouter]) -> None:
    router = router_factory()
    order_id = _register_router_order(router)

    router.process_order_event(client_order_id=order_id, event="ack")
    router.process_order_event(client_order_id=order_id, event="canceled")

    assert router._order_tracker.get(order_id) is None
    stats = router.get_tracker_stats()
    assert stats["removed_terminal"] == 1


def test_cleanup_by_ttl(router_factory: Callable[[], SmartRouter]) -> None:
    router = router_factory()
    first_id = _register_router_order(router, nonce=1)
    second_id = _register_router_order(router, nonce=2)

    ttl_seconds = tracker_ttl_seconds()
    assert ttl_seconds > 0
    reference = time.time()
    stale = router._order_tracker.get(first_id)
    fresh = router._order_tracker.get(second_id)
    assert stale is not None
    assert fresh is not None
    stale_ts = reference - ttl_seconds - 1.0
    stale.updated_ts = stale_ts
    stale.updated_ns = int(stale_ts * 1_000_000_000)
    fresh.updated_ts = reference
    fresh.updated_ns = int(reference * 1_000_000_000)

    removed = router.cleanup_tracker_by_ttl(reference)

    assert removed == 1
    assert router._order_tracker.get(first_id) is None
    assert router._order_tracker.get(second_id) is not None
    assert router.get_tracker_stats()["removed_ttl"] == 1


def test_cleanup_by_size(
    router_factory: Callable[[], SmartRouter],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = router_factory()
    base_ns = time.time_ns()
    order_ids: list[str] = []
    for idx in range(7):
        order_id = _register_router_order(
            router,
            ts_ns=base_ns + idx,
            nonce=idx + 1,
        )
        order_ids.append(order_id)
        tracked = router._order_tracker.get(order_id)
        assert tracked is not None
        updated_ts = (base_ns + idx) / 1_000_000_000
        tracked.updated_ts = updated_ts
        tracked.updated_ns = base_ns + idx

    monkeypatch.setenv("TRACKER_MAX_ITEMS", "5")
    removed = router.cleanup_tracker_by_size()

    assert removed == 2
    remaining = {snapshot.coid for snapshot in router._order_tracker.snapshot()}
    assert remaining == set(order_ids[-5:])
    assert router.get_tracker_stats()["removed_size"] == 2
