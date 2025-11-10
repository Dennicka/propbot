"""Prometheus metrics that describe runtime SLO/observability signals."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from time import perf_counter
from typing import Iterator

from prometheus_client import Counter, Gauge, Histogram


LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric definitions

ORDER_CYCLE_HISTOGRAM = Histogram(
    "propbot_order_cycle_ms",
    "Order execution/request cycle latency",
)

WS_GAP_HISTOGRAM = Histogram(
    "propbot_ws_gap_ms",
    "Market data gap between updates",
)
WS_GAP_HISTOGRAM.observe(0.0)

SKIPPED_COUNTER = Counter(
    "propbot_skipped_by_reason_total",
    "Skipped executions by reason",
    ("reason",),
)

WATCHDOG_OK_GAUGE = Gauge(
    "propbot_watchdog_ok",
    "Watchdog overall ok",
    ("exchange",),
)

DAILY_LOSS_BREACHED_GAUGE = Gauge(
    "propbot_daily_loss_breached",
    "Daily loss breached (0/1)",
)
DAILY_LOSS_BREACHED_GAUGE.set(0.0)

# ---------------------------------------------------------------------------
# Helper functions

_VALID_REASONS = {
    "risk_gate": "risk_gate",
    "daily_loss_cap": "daily_loss_cap",
    "universe": "universe",
    "watchdog": "watchdog",
    "hold": "hold",
}


def _normalise_reason(reason: str | None) -> str:
    value = (reason or "").strip().lower()
    if value in _VALID_REASONS:
        return _VALID_REASONS[value]
    if value.startswith("risk"):
        return "risk_gate"
    if "daily" in value or "loss" in value:
        return "daily_loss_cap"
    if value.startswith("universe"):
        return "universe"
    if value.startswith("watchdog"):
        return "watchdog"
    if value.startswith("hold"):
        return "hold"
    return "risk_gate"


def inc_skipped(reason: str | None) -> None:
    """Increment the skipped counter for ``reason``.

    Unknown reasons are mapped onto the closest configured label in order to
    keep the Prometheus cardinality bounded.
    """

    label = _normalise_reason(reason)
    SKIPPED_COUNTER.labels(reason=label).inc()


def record_order_cycle(duration_ms: float | None) -> None:
    """Record an explicit order cycle duration in milliseconds."""

    if duration_ms is None:
        return
    try:
        duration_value = float(duration_ms)
    except (TypeError, ValueError):
        return
    if duration_value < 0:
        duration_value = 0.0
    ORDER_CYCLE_HISTOGRAM.observe(duration_value)


@contextmanager
def order_cycle_timer() -> Iterator[None]:
    """Context manager that records elapsed time into the order cycle histogram."""

    start = perf_counter()
    try:
        yield
    finally:
        record_order_cycle((perf_counter() - start) * 1000.0)


def observe_ws_gap(gap_ms: float | None) -> None:
    """Observe a websocket gap duration (defaults to zero for placeholders)."""

    if gap_ms is None:
        WS_GAP_HISTOGRAM.observe(0.0)
        return
    try:
        gap_value = float(gap_ms)
    except (TypeError, ValueError):
        gap_value = 0.0
    if gap_value < 0:
        gap_value = 0.0
    WS_GAP_HISTOGRAM.observe(gap_value)


def set_watchdog_ok(exchange: str | None, ok: bool) -> None:
    """Update the watchdog gauge for ``exchange``."""

    label = (exchange or "unknown").strip() or "unknown"
    WATCHDOG_OK_GAUGE.labels(exchange=label).set(1.0 if ok else 0.0)


def set_daily_loss_breached(breached: bool) -> None:
    """Update the daily loss breached gauge."""

    DAILY_LOSS_BREACHED_GAUGE.set(1.0 if breached else 0.0)


def reset_for_tests() -> None:  # pragma: no cover - used only in tests
    """Reset metric state to a deterministic baseline."""

    try:
        SKIPPED_COUNTER._metrics.clear()  # type: ignore[attr-defined]
    except Exception as exc:
        LOGGER.debug("failed to reset skipped counter error=%s", exc)
    try:
        WATCHDOG_OK_GAUGE._metrics.clear()  # type: ignore[attr-defined]
    except Exception as exc:
        LOGGER.debug("failed to reset watchdog gauge error=%s", exc)
    for collector in (ORDER_CYCLE_HISTOGRAM, WS_GAP_HISTOGRAM):
        try:
            collector._sum.set(0.0)  # type: ignore[attr-defined]
            collector._count.set(0.0)  # type: ignore[attr-defined]
            for bucket in collector._buckets:  # type: ignore[attr-defined]
                bucket.set(0.0)
        except Exception as exc:
            LOGGER.debug("failed to reset slo histogram collector=%s error=%s", collector, exc)
    DAILY_LOSS_BREACHED_GAUGE.set(0.0)
    WS_GAP_HISTOGRAM.observe(0.0)


__all__ = [
    "ORDER_CYCLE_HISTOGRAM",
    "WS_GAP_HISTOGRAM",
    "SKIPPED_COUNTER",
    "WATCHDOG_OK_GAUGE",
    "DAILY_LOSS_BREACHED_GAUGE",
    "inc_skipped",
    "order_cycle_timer",
    "observe_ws_gap",
    "record_order_cycle",
    "set_watchdog_ok",
    "set_daily_loss_breached",
    "reset_for_tests",
]
