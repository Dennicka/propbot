from app.execution.order_state import (
    OrderState,
    OrderStatus,
    apply_exchange_update,
)


def test_progression_from_new_to_filled() -> None:
    state = OrderState(status=OrderStatus.NEW, qty=5.0)

    state = apply_exchange_update(state, {"status": "ack"})
    assert state.status == OrderStatus.ACK
    assert state.cum_filled == 0.0

    state = apply_exchange_update(
        state,
        {"status": "partial_fill", "cumQty": "2.0", "trade_id": "t1"},
    )
    assert state.status == OrderStatus.PARTIAL
    assert state.cum_filled == 2.0

    state = apply_exchange_update(
        state,
        {"status": "filled", "cumQty": 5, "trade_id": "t2", "avg_price": 100},
    )
    assert state.status == OrderStatus.FILLED
    assert state.cum_filled == 5.0
    assert state.avg_price == 100.0


def test_cancel_after_partial() -> None:
    state = OrderState(status=OrderStatus.PARTIAL, qty=10.0, cum_filled=3.0)
    state = apply_exchange_update(state, {"status": "cancelled"})
    assert state.status == OrderStatus.CANCELED
    assert state.cum_filled == 3.0


def test_duplicate_updates_are_idempotent() -> None:
    state = OrderState(status=OrderStatus.ACK, qty=4.0)
    ack = apply_exchange_update(state, {"status": "ack"})
    assert ack.status == OrderStatus.ACK
    assert ack.cum_filled == 0.0

    partial = apply_exchange_update(
        ack,
        {
            "status": "partial_fill",
            "cumQty": 2,
            "trade_id": "fill-1",
            "avg_price": 50.0,
        },
    )
    assert partial.status == OrderStatus.PARTIAL
    assert partial.cum_filled == 2.0

    duplicate = apply_exchange_update(
        partial,
        {
            "status": "filled",
            "cumQty": 2,
            "trade_id": "fill-1",
        },
    )
    assert duplicate.status == OrderStatus.PARTIAL
    assert duplicate.cum_filled == 2.0
    assert duplicate.last_event == "duplicate_fill_ignored"

    final = apply_exchange_update(
        duplicate,
        {
            "status": "filled",
            "cumQty": 4,
            "trade_id": "fill-2",
        },
    )
    assert final.status == OrderStatus.FILLED
    assert final.cum_filled == 4.0
    assert final.last_event is None

