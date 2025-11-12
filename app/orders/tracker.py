"""Order lifecycle tracking with bounded memory usage."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable

from .state import OrderState, next_state

LOGGER = logging.getLogger(__name__)

TRACKER_TTL_SEC = 3600
TRACKER_MAX_ACTIVE = 5000
_NANOS_IN_SECOND = 1_000_000_000


def _to_decimal(value: Decimal | float | int | str | None) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
        LOGGER.error(
            "order_tracker.invalid_decimal",
            extra={
                "event": "order_tracker_invalid_decimal",
                "component": "orders_tracker",
                "details": {"value": repr(value)},
            },
            exc_info=exc,
        )
        raise


@dataclass(slots=True)
class TrackedOrder:
    coid: str
    venue: str
    symbol: str
    side: str
    qty: Decimal
    filled: Decimal = Decimal("0")
    state: OrderState = OrderState.NEW
    created_ns: int = 0
    updated_ns: int = 0


class OrderTracker:
    """Maintain a compact mapping of order states."""

    def __init__(self, *, max_active: int = TRACKER_MAX_ACTIVE) -> None:
        self._orders: Dict[str, TrackedOrder] = {}
        self._max_active = max_active if max_active > 0 else TRACKER_MAX_ACTIVE

    def __len__(self) -> int:
        return len(self._orders)

    def get(self, coid: str) -> TrackedOrder | None:
        return self._orders.get(coid)

    def register_order(
        self,
        coid: str,
        *,
        venue: str,
        symbol: str,
        side: str,
        qty: Decimal,
        now_ns: int,
    ) -> None:
        qty_value = _to_decimal(qty)
        if qty_value < Decimal("0"):
            qty_value = Decimal("0")
        existing = self._orders.get(coid)
        if existing is not None:
            existing.venue = venue
            existing.symbol = symbol
            existing.side = side
            existing.qty = qty_value
            existing.filled = min(existing.filled, qty_value)
            existing.updated_ns = now_ns
            return
        tracked = TrackedOrder(
            coid=coid,
            venue=venue,
            symbol=symbol,
            side=side,
            qty=qty_value,
            created_ns=now_ns,
            updated_ns=now_ns,
        )
        self._orders[coid] = tracked
        self._enforce_capacity()

    def apply_event(
        self,
        coid: str,
        event: str,
        qty: Decimal | None,
        now_ns: int,
    ) -> OrderState:
        tracked = self._orders.get(coid)
        if tracked is None:
            LOGGER.error(
                "order_tracker.unknown_order",
                extra={
                    "event": "order_tracker_unknown_order",
                    "component": "orders_tracker",
                    "details": {"coid": coid, "event": event},
                },
            )
            raise KeyError(f"unknown order: {coid}")
        event_key = event.strip().lower()
        if not event_key:
            LOGGER.error(
                "order_tracker.empty_event",
                extra={
                    "event": "order_tracker_empty_event",
                    "component": "orders_tracker",
                    "details": {"coid": coid},
                },
            )
            raise ValueError("event must be a non-empty string")
        if event_key == "expired":
            event_key = "expire"
        new_state = next_state(tracked.state, event_key)
        increment = Decimal("0")
        if event_key in {"partial_fill", "filled"}:
            if qty is not None:
                increment = _to_decimal(qty)
            if increment <= Decimal("0") and event_key == "filled":
                increment = tracked.qty - tracked.filled
            if increment < Decimal("0"):
                increment = Decimal("0")
            tracked.filled = min(tracked.qty, tracked.filled + increment)
        if new_state == OrderState.FILLED:
            tracked.filled = tracked.qty
        tracked.state = new_state
        tracked.updated_ns = now_ns
        return tracked.state

    @staticmethod
    def is_terminal(state: OrderState) -> bool:
        return state in {
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }

    def prune_terminal(self) -> int:
        if not self._orders:
            return 0
        removed = 0
        terminal_ids = [
            coid for coid, tracked in self._orders.items() if self.is_terminal(tracked.state)
        ]
        for coid in terminal_ids:
            self._orders.pop(coid, None)
            removed += 1
        return removed

    def prune_aged(self, now_ns: int, ttl_sec: int) -> int:
        if ttl_sec <= 0 or not self._orders:
            return 0
        ttl_ns = ttl_sec * _NANOS_IN_SECOND
        removed = 0
        aged_ids = [
            coid for coid, tracked in self._orders.items() if now_ns - tracked.updated_ns > ttl_ns
        ]
        for coid in aged_ids:
            self._orders.pop(coid, None)
            removed += 1
        return removed

    def _enforce_capacity(self) -> None:
        if len(self._orders) <= self._max_active:
            return
        terminal_orders: Iterable[TrackedOrder] = (
            tracked for tracked in self._orders.values() if self.is_terminal(tracked.state)
        )
        for tracked in sorted(terminal_orders, key=lambda item: item.updated_ns):
            if len(self._orders) <= self._max_active:
                break
            self._orders.pop(tracked.coid, None)
        if len(self._orders) > self._max_active:
            LOGGER.warning(
                "order_tracker.capacity_exceeded",
                extra={
                    "event": "order_tracker_capacity_exceeded",
                    "component": "orders_tracker",
                    "details": {
                        "max_active": self._max_active,
                        "current_active": len(self._orders),
                    },
                },
            )


__all__ = [
    "OrderTracker",
    "TrackedOrder",
    "TRACKER_TTL_SEC",
    "TRACKER_MAX_ACTIVE",
]
