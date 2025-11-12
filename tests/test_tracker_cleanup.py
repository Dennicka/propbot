from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from app.orders.state import OrderState
from app.orders.tracker import (
    OrderTracker,
    reset_tracker_metrics,
    tracker_metrics_snapshot,
)


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
