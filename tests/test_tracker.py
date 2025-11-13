import time

import pytest

from app.orders.tracker import OrderTracker


def test_terminal_eviction() -> None:
    tracker = OrderTracker()
    tracker.register_order(
        "order-1",
        key="intent-1",
        venue="X",
        symbol="BTCUSDT",
        side="buy",
        ts=10.0,
    )

    tracker.process_order_event(
        "order-1",
        "FILLED",
        ts=11.0,
        venue="X",
        symbol="BTCUSDT",
        side="buy",
    )

    assert len(tracker) == 0
    assert tracker.stats["removed_terminal"] == 1


def test_ttl_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = OrderTracker(ttl_seconds=1, max_items=10)
    tracker.register_order(
        "order-ttl",
        key="intent-ttl",
        venue="Y",
        symbol="ETHUSDT",
        side="sell",
        ts=100.0,
    )

    monkeypatch.setattr("app.orders.tracker.time", lambda: 102.0)
    tracker.cleanup()

    assert len(tracker) == 0
    assert tracker.stats["removed_ttl"] == 1


def test_size_cap() -> None:
    tracker = OrderTracker(ttl_seconds=1000, max_items=1)
    base = time.time()
    tracker.register_order(
        "first",
        key="intent-first",
        venue="Z",
        symbol="LTCUSDT",
        side="buy",
        ts=base,
    )
    tracker.register_order(
        "second",
        key="intent-second",
        venue="Z",
        symbol="LTCUSDT",
        side="buy",
        ts=base + 1.0,
    )

    tracker.cleanup()

    assert len(tracker) == 1
    assert tracker.get("second") is not None
    assert tracker.stats["removed_size"] == 1
