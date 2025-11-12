from __future__ import annotations

import pytest

from app.orders.state import OrderState, next_state


def test_valid_transition_sequence() -> None:
    state = OrderState.NEW
    state = next_state(state, "submit")
    assert state == OrderState.PENDING
    state = next_state(state, "ack")
    assert state == OrderState.ACK
    state = next_state(state, "partial_fill")
    assert state == OrderState.PARTIAL
    # Repeated partial fills retain PARTIAL state
    state = next_state(state, "partial_fill")
    assert state == OrderState.PARTIAL
    state = next_state(state, "filled")
    assert state == OrderState.FILLED
    state = next_state(state, "canceled")
    assert state == OrderState.CANCELED


def test_invalid_transition_raises_value_error() -> None:
    with pytest.raises(ValueError):
        next_state(OrderState.NEW, "ack")
    with pytest.raises(ValueError):
        next_state(OrderState.ACK, "submit")
    with pytest.raises(ValueError):
        next_state(OrderState.PENDING, "filled")
    with pytest.raises(ValueError):
        next_state(OrderState.PARTIAL, "")
