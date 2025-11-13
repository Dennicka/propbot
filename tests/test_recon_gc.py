"""Unit tests for terminal order garbage collection."""

from decimal import Decimal

from app.orders.state import OrderState
from app.orders.tracker import OrderTracker


def _register(tracker: OrderTracker, order_id: str, *, now_ns: int) -> None:
    tracker.register_order(
        order_id,
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=Decimal("1"),
        now_ns=now_ns,
    )


def test_purge_terminated_orders_respects_ttl() -> None:
    tracker = OrderTracker()
    base_ns = 0
    _register(tracker, "active", now_ns=base_ns)
    _register(tracker, "fresh-final", now_ns=base_ns)
    _register(tracker, "old-final", now_ns=base_ns)

    tracker.mark_terminal("fresh-final", OrderState.FILLED, ts=50.0)
    tracker.mark_terminal("old-final", OrderState.CANCELED, ts=10.0)

    removed = tracker.purge_terminated_older_than(ttl_sec=30, now=70.0)

    assert removed == 1
    assert tracker.get("old-final") is None
    assert tracker.get("fresh-final") is not None
    assert tracker.get("active") is not None


def test_purge_terminated_orders_leaves_active_states() -> None:
    tracker = OrderTracker()
    _register(tracker, "partial", now_ns=0)
    _register(tracker, "final", now_ns=0)

    tracker.apply_event("partial", "submit", None, now_ns=5)
    tracker.apply_event("partial", "ack", None, now_ns=10)
    tracker.apply_event("partial", "partial_fill", Decimal("0.5"), now_ns=20)
    tracker.mark_terminal("final", OrderState.REJECTED, ts=1.0)

    removed = tracker.purge_terminated_older_than(ttl_sec=10, now=8.0)

    assert removed == 0
    assert tracker.get("final") is not None
    assert tracker.get("partial") is not None

    removed_late = tracker.purge_terminated_older_than(ttl_sec=10, now=30.0)

    assert removed_late == 1
    assert tracker.get("final") is None
    assert tracker.get("partial") is not None
