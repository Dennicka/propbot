from app.execution.order_state import (
    OrderState,
    OrderStatus,
    apply_exchange_update,
)


def test_duplicate_fill_does_not_increase_totals() -> None:
    state = OrderState(status=OrderStatus.ACK, qty=3.0)

    first_fill = apply_exchange_update(
        state,
        {"last_fill_qty": 1.0, "trade_id": "fill-1", "avg_price": 100.0},
    )
    assert first_fill.cum_filled == 1.0
    assert first_fill.status == OrderStatus.PARTIAL

    duplicate = apply_exchange_update(
        first_fill,
        {"last_fill_qty": 1.0, "trade_id": "fill-1", "avg_price": 100.0},
    )
    assert duplicate.cum_filled == 1.0
    assert duplicate.status == OrderStatus.PARTIAL
    assert duplicate.last_event == "duplicate_fill_ignored"

    second_fill = apply_exchange_update(
        duplicate,
        {"last_fill_qty": 2.0, "trade_id": "fill-2", "avg_price": 102.0},
    )
    assert second_fill.cum_filled == 3.0
    assert second_fill.status == OrderStatus.FILLED
    assert second_fill.last_event is None

