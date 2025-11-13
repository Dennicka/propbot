"""Order lifecycle tracking with bounded memory usage."""

from __future__ import annotations

import logging
from collections import Counter as _Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, Tuple

from prometheus_client import Counter, Gauge

from .state import OrderState, next_state

LOGGER = logging.getLogger(__name__)

TRACKER_TTL_SEC = 3600
TRACKER_MAX_ACTIVE = 5000
_NANOS_IN_SECOND = 1_000_000_000


FINAL_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
)


class _TrackerMetrics:
    """Capture tracker specific telemetry with optional Prometheus export."""

    def __init__(self) -> None:
        self._orders_tracked = Gauge(
            "orders_tracked",
            "Number of active orders tracked in memory",
        )
        self._orders_finalized = Counter(
            "orders_finalized_total",
            "Count of finalized orders partitioned by state",
            labelnames=("state",),
        )
        self._tracked_count = 0
        self._finalized_counts: _Counter[str] = _Counter()
        self._orders_tracked.set(0.0)

    def observe_tracked(self, count: int) -> None:
        """Update the tracked orders gauge and cached count."""

        self._tracked_count = max(0, count)
        self._orders_tracked.set(float(self._tracked_count))

    def observe_finalized(self, state: OrderState) -> None:
        """Record a finalized order for the provided terminal state."""

        label = state.value.lower()
        self._finalized_counts[label] += 1
        self._orders_finalized.labels(state=label).inc()

    def snapshot(self) -> Dict[str, object]:
        """Return a snapshot of the collected metrics for tests."""

        return {
            "tracked": self._tracked_count,
            "finalized": dict(self._finalized_counts),
        }

    def reset(self) -> None:
        """Reset cached metric values for deterministic unit tests."""

        self._tracked_count = 0
        self._finalized_counts.clear()
        self._orders_tracked.set(0.0)


_TRACKER_METRICS = _TrackerMetrics()


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
    updated_ts: float = 0.0


@dataclass(slots=True, frozen=True)
class TrackedOrderSnapshot:
    """Immutable view of a tracked order."""

    coid: str
    venue: str
    symbol: str
    side: str
    qty: Decimal
    filled: Decimal
    state: OrderState
    created_ns: int
    updated_ns: int
    updated_ts: float


class OrderTracker:
    """Maintain a compact mapping of order states."""

    def __init__(self, *, max_active: int = TRACKER_MAX_ACTIVE) -> None:
        self._orders: Dict[str, TrackedOrder] = {}
        self._max_active = max_active if max_active > 0 else TRACKER_MAX_ACTIVE
        _TRACKER_METRICS.observe_tracked(len(self._orders))

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
        """Register a new order for lifecycle tracking.

        Duplicate registrations are ignored to guarantee idempotency.
        """

        qty_value = _to_decimal(qty)
        if qty_value < Decimal("0"):
            qty_value = Decimal("0")
        existing = self._orders.get(coid)
        if existing is not None:
            LOGGER.warning(
                "order_tracker.duplicate_registration",
                extra={
                    "event": "order_tracker_duplicate_registration",
                    "component": "orders_tracker",
                    "details": {
                        "coid": coid,
                        "venue": venue,
                        "symbol": symbol,
                    },
                },
            )
            return
        now_ts = float(now_ns) / _NANOS_IN_SECOND
        tracked = TrackedOrder(
            coid=coid,
            venue=venue,
            symbol=symbol,
            side=side,
            qty=qty_value,
            created_ns=now_ns,
            updated_ns=now_ns,
            updated_ts=now_ts,
        )
        self._orders[coid] = tracked
        self._enforce_capacity()
        _TRACKER_METRICS.observe_tracked(len(self._orders))

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
        tracked.updated_ts = float(now_ns) / _NANOS_IN_SECOND
        return tracked.state

    @staticmethod
    def is_terminal(state: OrderState) -> bool:
        return state in FINAL_STATES

    def mark_terminal(self, coid: str, state: OrderState, ts: float) -> bool:
        """Mark an order as terminal without removing it from the tracker."""

        tracked = self._orders.get(coid)
        if tracked is None:
            return False
        if not self.is_terminal(state):
            raise ValueError(f"state {state!s} is not terminal")
        previous_state = tracked.state
        tracked.state = state
        tracked.updated_ts = max(float(ts), float(tracked.created_ns) / _NANOS_IN_SECOND)
        updated_ns = int(max(tracked.updated_ts, 0.0) * _NANOS_IN_SECOND)
        if updated_ns >= tracked.created_ns:
            tracked.updated_ns = updated_ns
        if state is OrderState.FILLED:
            tracked.filled = tracked.qty
        if not self.is_terminal(previous_state):
            _TRACKER_METRICS.observe_finalized(state)
        return True

    def finalize(self, coid: str, state: OrderState) -> bool:
        """Remove a finalized order and emit telemetry.

        Returns ``True`` when the order was tracked and removed.
        """

        tracked = self._orders.pop(coid, None)
        if tracked is None:
            return False
        final_state = tracked.state if self.is_terminal(tracked.state) else state
        if not self.is_terminal(final_state):
            raise ValueError(f"state {state!s} is not terminal")
        LOGGER.debug(
            "order_tracker.finalized",
            extra={
                "event": "order_tracker_finalized",
                "component": "orders_tracker",
                "details": {"coid": coid, "state": final_state.value},
            },
        )
        _TRACKER_METRICS.observe_finalized(final_state)
        _TRACKER_METRICS.observe_tracked(len(self._orders))
        return True

    def prune_terminal(self) -> int:
        if not self._orders:
            return 0
        removed = 0
        terminal_ids = [
            coid for coid, tracked in self._orders.items() if self.is_terminal(tracked.state)
        ]
        for coid in terminal_ids:
            tracked = self._orders.get(coid)
            if tracked is None:
                continue
            if self.finalize(coid, tracked.state):
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
        if removed:
            _TRACKER_METRICS.observe_tracked(len(self._orders))
        return removed

    def purge_terminated_older_than(self, ttl_sec: int, now: float) -> int:
        """Remove terminal orders older than the provided TTL."""

        if ttl_sec <= 0 or not self._orders:
            return 0
        removed = 0
        reference = float(now)
        ttl = float(ttl_sec)
        for coid, tracked in tuple(self._orders.items()):
            if not self.is_terminal(tracked.state):
                continue
            last_update = tracked.updated_ts or float(tracked.updated_ns) / _NANOS_IN_SECOND
            if reference - last_update <= ttl:
                continue
            self._orders.pop(coid, None)
            removed += 1
        if removed:
            _TRACKER_METRICS.observe_tracked(len(self._orders))
        return removed

    def snapshot(self) -> Tuple[TrackedOrderSnapshot, ...]:
        """Return an immutable snapshot of the current tracked orders."""

        return tuple(
            TrackedOrderSnapshot(
                coid=item.coid,
                venue=item.venue,
                symbol=item.symbol,
                side=item.side,
                qty=item.qty,
                filled=item.filled,
                state=item.state,
                created_ns=item.created_ns,
                updated_ns=item.updated_ns,
                updated_ts=item.updated_ts,
            )
            for item in self._orders.values()
        )

    def _enforce_capacity(self) -> None:
        if len(self._orders) <= self._max_active:
            return
        terminal_orders: Iterable[TrackedOrder] = (
            tracked for tracked in self._orders.values() if self.is_terminal(tracked.state)
        )
        for tracked in sorted(terminal_orders, key=lambda item: item.updated_ns):
            if len(self._orders) <= self._max_active:
                break
            self.finalize(tracked.coid, tracked.state)
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
        _TRACKER_METRICS.observe_tracked(len(self._orders))


def tracker_metrics_snapshot() -> Dict[str, object]:
    """Expose tracker telemetry for unit tests."""

    return _TRACKER_METRICS.snapshot()


def reset_tracker_metrics() -> None:
    """Reset cached tracker telemetry for unit tests."""

    _TRACKER_METRICS.reset()


__all__ = [
    "OrderTracker",
    "TrackedOrder",
    "TrackedOrderSnapshot",
    "TRACKER_TTL_SEC",
    "TRACKER_MAX_ACTIVE",
    "reset_tracker_metrics",
    "tracker_metrics_snapshot",
]
