"""Order lifecycle tracking with bounded memory usage."""

from __future__ import annotations

import logging
import os
from collections import Counter as _Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from time import time
from typing import Dict, Iterable, Optional, Tuple

from prometheus_client import Counter, Gauge

from .state import OrderState, next_state

LOGGER = logging.getLogger(__name__)

TRACKER_TTL_SEC = 3600
TRACKER_MAX_ACTIVE = 5000
_NANOS_IN_SECOND = 1_000_000_000

_DEFAULT_TRACKER_TTL_SECONDS = 3_600
_DEFAULT_TRACKER_MAX_ITEMS = 20_000


def _read_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(value, 0)


TRACKER_TTL_SECONDS = _read_positive_int("TRACKER_TTL_SECONDS", _DEFAULT_TRACKER_TTL_SECONDS)
TRACKER_MAX_ITEMS = _read_positive_int("TRACKER_MAX_ITEMS", _DEFAULT_TRACKER_MAX_ITEMS)


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


def _coerce_state(value: OrderState | str) -> OrderState:
    if isinstance(value, OrderState):
        return value
    key = str(value).strip().upper()
    if not key:
        raise ValueError("state must be a non-empty string")
    try:
        return OrderState(key)
    except ValueError as exc:
        LOGGER.error(
            "order_tracker.invalid_state",
            extra={
                "event": "order_tracker_invalid_state",
                "component": "orders_tracker",
                "details": {"state": key},
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
    key: str = ""


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

    def __init__(
        self,
        *,
        max_active: int | None = None,
        ttl_seconds: int = TRACKER_TTL_SECONDS,
        max_items: int = TRACKER_MAX_ITEMS,
    ) -> None:
        self._orders: Dict[str, TrackedOrder] = {}
        effective_max_active = TRACKER_MAX_ACTIVE if max_active is None else max(max_active, 0)
        self._max_active = effective_max_active or TRACKER_MAX_ACTIVE
        self._ttl_seconds = max(int(ttl_seconds), 0)
        self._max_items = max(int(max_items), 0)
        self.stats: Dict[str, int] = {
            "added": 0,
            "updates": 0,
            "removed_terminal": 0,
            "removed_ttl": 0,
            "removed_size": 0,
        }
        _TRACKER_METRICS.observe_tracked(len(self._orders))

    def __len__(self) -> int:
        return len(self._orders)

    def get(self, coid: str) -> TrackedOrder | None:
        return self._orders.get(coid)

    def register_order(self, coid: str, key: str = "", **ctx) -> None:
        """Register a new order for lifecycle tracking.

        Duplicate registrations are ignored to guarantee idempotency.
        """

        existing = self._orders.get(coid)
        venue = str(ctx.get("venue", getattr(existing, "venue", "")))
        symbol = str(ctx.get("symbol", getattr(existing, "symbol", "")))
        side = str(ctx.get("side", getattr(existing, "side", "")))
        qty_raw = ctx.get("qty", ctx.get("quantity", Decimal("0")))
        qty_value = _to_decimal(qty_raw)
        if qty_value < Decimal("0"):
            qty_value = Decimal("0")
        ts_value = ctx.get("ts")
        now_ns = ctx.get("now_ns")
        if ts_value is not None:
            now_ts = float(ts_value)
        elif now_ns is not None:
            now_ts = float(now_ns) / _NANOS_IN_SECOND
        else:
            now_ts = time()
        if now_ns is None:
            now_ns = int(now_ts * _NANOS_IN_SECOND)
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
        tracked = TrackedOrder(
            coid=coid,
            venue=venue,
            symbol=symbol,
            side=side,
            qty=qty_value,
            created_ns=now_ns,
            updated_ns=now_ns,
            updated_ts=now_ts,
            key=key,
        )
        self._orders[coid] = tracked
        self._enforce_capacity()
        self.stats["added"] += 1
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

    def process_order_event(
        self,
        coid: str,
        new_state: OrderState | str,
        **ctx,
    ) -> None:
        state = _coerce_state(new_state)
        ts_value = ctx.get("ts")
        now_ns = ctx.get("now_ns")
        if ts_value is not None:
            now_ts = float(ts_value)
        elif now_ns is not None:
            now_ts = float(now_ns) / _NANOS_IN_SECOND
        else:
            now_ts = time()
        if now_ns is None:
            now_ns = int(now_ts * _NANOS_IN_SECOND)
        entry = self._orders.get(coid)
        if state in FINAL_STATES:
            if entry is not None:
                entry.state = state
                entry.updated_ts = now_ts
                entry.updated_ns = now_ns
                entry.venue = str(ctx.get("venue", entry.venue))
                entry.symbol = str(ctx.get("symbol", entry.symbol))
                entry.side = str(ctx.get("side", entry.side))
                entry.key = str(ctx.get("key", entry.key))
            removed = self.finalize(coid, state)
            if not removed and entry is not None:
                self._orders.pop(coid, None)
                _TRACKER_METRICS.observe_tracked(len(self._orders))
            self.stats["removed_terminal"] += 1
            return
        if entry is None:
            qty_value = _to_decimal(ctx.get("qty", ctx.get("quantity", Decimal("0"))))
            if qty_value < Decimal("0"):
                qty_value = Decimal("0")
            entry = TrackedOrder(
                coid=coid,
                venue=str(ctx.get("venue", "")),
                symbol=str(ctx.get("symbol", "")),
                side=str(ctx.get("side", "")),
                qty=qty_value,
                state=state,
                created_ns=now_ns,
                updated_ns=now_ns,
                updated_ts=now_ts,
                key=str(ctx.get("key", "")),
            )
            self._orders[coid] = entry
            self._enforce_capacity()
            _TRACKER_METRICS.observe_tracked(len(self._orders))
        entry.state = state
        entry.updated_ts = now_ts
        entry.updated_ns = now_ns
        entry.venue = str(ctx.get("venue", entry.venue))
        entry.symbol = str(ctx.get("symbol", entry.symbol))
        entry.side = str(ctx.get("side", entry.side))
        entry.key = str(ctx.get("key", entry.key))
        self.stats["updates"] += 1

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

    def cleanup(
        self,
        now: Optional[float] = None,
        *,
        ttl_seconds: Optional[int] = None,
        max_items: Optional[int] = None,
    ) -> tuple[list[tuple[str, OrderState]], list[tuple[str, OrderState]]]:
        reference = float(now) if now is not None else time()
        if ttl_seconds is None:
            ttl_limit = self._ttl_seconds
        else:
            ttl_limit = int(ttl_seconds)
        if max_items is None:
            size_limit = self._max_items
        else:
            size_limit = int(max_items)
        removed_ttl: list[tuple[str, OrderState]] = []
        removed_size: list[tuple[str, OrderState]] = []
        if ttl_limit is not None and ttl_limit > 0 and self._orders:
            removed_ttl = TrackingCleaner.cleanup_by_ttl(
                self,
                now_ts=reference,
                ttl_seconds=ttl_limit,
            )
        if size_limit is not None and size_limit >= 0 and self._orders:
            removed_size = TrackingCleaner.cleanup_by_size(
                self,
                max_items=size_limit,
            )
        if removed_ttl:
            self.stats["removed_ttl"] += len(removed_ttl)
        if removed_size:
            self.stats["removed_size"] += len(removed_size)
        if removed_ttl or removed_size:
            LOGGER.info(
                "tracker.cleanup ttl=%d size=%d",
                len(removed_ttl),
                len(removed_size),
            )
        return removed_ttl, removed_size

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


def tracker_ttl_seconds() -> int:
    """Return the configured TTL for tracker entries in seconds."""

    return _read_positive_int("TRACKER_TTL_SECONDS", _DEFAULT_TRACKER_TTL_SECONDS)


def tracker_max_items() -> int:
    """Return the configured maximum number of tracker entries."""

    return _read_positive_int("TRACKER_MAX_ITEMS", _DEFAULT_TRACKER_MAX_ITEMS)


class TrackingCleaner:
    """Utility helpers for pruning tracker entries."""

    @staticmethod
    def cleanup_by_ttl(
        tracker: "OrderTracker",
        *,
        now_ts: float,
        ttl_seconds: int,
    ) -> list[tuple[str, OrderState]]:
        if ttl_seconds <= 0:
            return []
        reference = float(now_ts)
        ttl = float(ttl_seconds)
        removed: list[tuple[str, OrderState]] = []
        orders = tracker._orders  # noqa: SLF001 - internal coordination helper
        for coid, tracked in tuple(orders.items()):
            last_update = tracked.updated_ts or float(tracked.updated_ns) / _NANOS_IN_SECOND
            if reference - last_update <= ttl:
                continue
            removed.append((coid, tracked.state))
            orders.pop(coid, None)
        if removed:
            _TRACKER_METRICS.observe_tracked(len(orders))
        return removed

    @staticmethod
    def cleanup_by_size(
        tracker: "OrderTracker",
        *,
        max_items: int,
    ) -> list[tuple[str, OrderState]]:
        limit = max(int(max_items), 0)
        orders = tracker._orders  # noqa: SLF001 - internal coordination helper
        if not orders:
            return []
        if limit == 0:
            removed = [(coid, tracked.state) for coid, tracked in orders.items()]
            orders.clear()
            _TRACKER_METRICS.observe_tracked(0)
            return removed
        if len(orders) <= limit:
            return []
        sorted_orders = sorted(
            orders.items(),
            key=lambda item: (
                item[1].updated_ts or float(item[1].updated_ns) / _NANOS_IN_SECOND,
                item[1].created_ns,
            ),
        )
        to_remove = len(orders) - limit
        removed: list[tuple[str, OrderState]] = []
        for coid, tracked in sorted_orders[:to_remove]:
            removed.append((coid, tracked.state))
            orders.pop(coid, None)
        if removed:
            _TRACKER_METRICS.observe_tracked(len(orders))
        return removed


__all__ = [
    "OrderTracker",
    "TrackedOrder",
    "TrackedOrderSnapshot",
    "TRACKER_TTL_SEC",
    "TRACKER_MAX_ACTIVE",
    "TRACKER_TTL_SECONDS",
    "TRACKER_MAX_ITEMS",
    "TrackingCleaner",
    "tracker_max_items",
    "tracker_ttl_seconds",
    "reset_tracker_metrics",
    "tracker_metrics_snapshot",
]
