"""Unit tests for lifecycle guard validation."""

import pytest

from app.orders.state import OrderState, OrderStateError, validate_transition


@pytest.mark.parametrize(
    ("previous", "new"),
    (
        (OrderState.NEW, OrderState.FILLED),
        (OrderState.PENDING, OrderState.PARTIAL),
        (OrderState.ACK, OrderState.NEW),
        (OrderState.PARTIAL, OrderState.ACK),
    ),
)
def test_validate_transition_blocks_invalid_paths(previous: OrderState, new: OrderState) -> None:
    with pytest.raises(OrderStateError):
        validate_transition(previous, new)
