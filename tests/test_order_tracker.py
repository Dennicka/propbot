from decimal import Decimal

from app.orders.state import OrderState
from app.orders.tracker import OrderTracker


def _register_order(tracker: OrderTracker, coid: str, now_ns: int, qty: str = "5") -> None:
    tracker.register_order(
        coid,
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=Decimal(qty),
        now_ns=now_ns,
    )
    tracker.apply_event(coid, "submit", None, now_ns)


def test_tracker_clears_filled_orders() -> None:
    tracker = OrderTracker()
    coid = "order-filled"
    now = 1_000
    _register_order(tracker, coid, now)
    tracker.apply_event(coid, "ack", None, now + 1)
    tracker.apply_event(coid, "partial_fill", Decimal("2"), now + 2)
    tracker.apply_event(coid, "partial_fill", Decimal("2"), now + 3)
    state = tracker.apply_event(coid, "filled", None, now + 4)
    assert state == OrderState.FILLED
    assert tracker.prune_terminal() == 1
    assert tracker.get(coid) is None


def test_tracker_clears_canceled_orders() -> None:
    tracker = OrderTracker()
    coid = "order-canceled"
    now = 2_000
    _register_order(tracker, coid, now)
    tracker.apply_event(coid, "ack", None, now + 1)
    state = tracker.apply_event(coid, "canceled", None, now + 2)
    assert state == OrderState.CANCELED
    assert tracker.prune_terminal() == 1
    assert tracker.get(coid) is None


def test_tracker_clears_rejected_orders() -> None:
    tracker = OrderTracker()
    coid = "order-rejected"
    now = 3_000
    _register_order(tracker, coid, now)
    state = tracker.apply_event(coid, "reject", None, now + 1)
    assert state == OrderState.REJECTED
    assert tracker.prune_terminal() == 1
    assert tracker.get(coid) is None


def test_tracker_prunes_aged_orders() -> None:
    tracker = OrderTracker()
    coid = "order-aged"
    now = 4_000
    _register_order(tracker, coid, now)
    ttl_sec = 5
    ttl_ns = (ttl_sec + 1) * 1_000_000_000
    removed = tracker.prune_aged(now + ttl_ns, ttl_sec)
    assert removed == 1
    assert tracker.get(coid) is None


def test_tracker_enforces_capacity_with_terminal_orders() -> None:
    tracker = OrderTracker(max_active=3)
    base_ns = 5_000
    # Create three orders, two of which become terminal.
    for idx in range(3):
        coid = f"order-{idx}"
        _register_order(tracker, coid, base_ns + idx)
        tracker.apply_event(coid, "ack", None, base_ns + idx + 10)
        if idx < 2:
            tracker.apply_event(coid, "canceled", None, base_ns + idx + 20)
    assert len(tracker) == 3

    new_coid = "order-new"
    _register_order(tracker, new_coid, base_ns + 100)
    assert tracker.get(new_coid) is not None
    assert len(tracker) <= 3
    assert tracker.get("order-0") is None or tracker.get("order-1") is None
