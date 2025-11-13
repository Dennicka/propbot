"""Deterministic order state machine transitions."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Dict, Mapping

LOGGER = logging.getLogger(__name__)


class OrderState(str, Enum):
    """Enumerates supported order lifecycle states."""

    NEW = "NEW"
    PENDING = "PENDING"
    ACK = "ACK"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


# NOTE:
# The transition map is intentionally more permissive than the lifecycle guards
# exposed through ``validate_transition`` below.  The tracker relies on
# idempotent updates (for example, repeated ``partial_fill`` notifications)
# while the validator enforces the canonical order lifecycle expected by the
# routing layer.
_TRANSITIONS: Dict[OrderState, Dict[str, OrderState]] = {
    OrderState.NEW: {"submit": OrderState.PENDING, "canceled": OrderState.CANCELED},
    OrderState.PENDING: {
        "ack": OrderState.ACK,
        "canceled": OrderState.CANCELED,
        "reject": OrderState.REJECTED,
        "expire": OrderState.EXPIRED,
    },
    OrderState.ACK: {
        "partial_fill": OrderState.PARTIAL,
        "filled": OrderState.FILLED,
        "canceled": OrderState.CANCELED,
        "reject": OrderState.REJECTED,
        "expire": OrderState.EXPIRED,
    },
    OrderState.PARTIAL: {
        "partial_fill": OrderState.PARTIAL,
        "filled": OrderState.FILLED,
        "canceled": OrderState.CANCELED,
        "reject": OrderState.REJECTED,
        "expire": OrderState.EXPIRED,
    },
    OrderState.FILLED: {"canceled": OrderState.CANCELED},
    OrderState.CANCELED: {},
    OrderState.REJECTED: {},
    OrderState.EXPIRED: {},
}


def next_state(current: OrderState, event: str) -> OrderState:
    """Return the next state for the provided lifecycle event."""

    event_key = event.strip().lower()
    if not event_key:
        LOGGER.error(
            "order_state.invalid_transition",
            extra={
                "event": "order_state_invalid_transition",
                "component": "orders_state",
                "details": {
                    "current_state": current.value,
                    "event": event,
                    "reason": "empty_event",
                },
            },
        )
        raise ValueError("event must be a non-empty string")

    transitions = _TRANSITIONS.get(current, {})
    if event_key == "canceled" and current not in transitions:
        return OrderState.CANCELED

    next_state_value = transitions.get(event_key)
    if next_state_value is None:
        LOGGER.error(
            "order_state.invalid_transition",
            extra={
                "event": "order_state_invalid_transition",
                "component": "orders_state",
                "details": {
                    "current_state": current.value,
                    "event": event_key,
                    "reason": "disallowed",
                },
            },
        )
        raise ValueError(f"transition from {current.value} with event '{event}' is not allowed")
    return next_state_value


class OrderStateError(ValueError):
    """Raised when an invalid lifecycle transition is attempted."""


FINAL_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
)


ALLOWED_TRANSITIONS: Mapping[OrderState, frozenset[OrderState]] = {
    OrderState.NEW: frozenset({OrderState.PENDING, OrderState.REJECTED, OrderState.CANCELED}),
    OrderState.PENDING: frozenset({OrderState.ACK, OrderState.REJECTED, OrderState.EXPIRED}),
    OrderState.ACK: frozenset(
        {
            OrderState.PARTIAL,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
            OrderState.REJECTED,
        }
    ),
    OrderState.PARTIAL: frozenset(
        {
            OrderState.PARTIAL,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.EXPIRED,
        }
    ),
}


def validate_transition(previous: OrderState, new: OrderState) -> None:
    """Validate lifecycle transitions and raise ``OrderStateError`` on failure."""

    if previous == new:
        return
    if previous in FINAL_STATES:
        raise OrderStateError(
            f"transition from final state {previous.value} to {new.value} is not allowed"
        )
    allowed = ALLOWED_TRANSITIONS.get(previous)
    if allowed is None:
        raise OrderStateError(f"unknown state {previous.value}")
    if new not in allowed:
        raise OrderStateError(f"transition from {previous.value} to {new.value} is not allowed")


__all__ = [
    "ALLOWED_TRANSITIONS",
    "FINAL_STATES",
    "OrderState",
    "OrderStateError",
    "next_state",
    "validate_transition",
]
