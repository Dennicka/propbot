"""Deterministic order state machine transitions."""

from __future__ import annotations

import logging
from enum import Enum
from typing import Dict

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


__all__ = ["OrderState", "next_state"]
