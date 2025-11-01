"""Prometheus metrics underpinning SLOs and alerting rules."""

from __future__ import annotations

from typing import Iterable

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "API_REQUEST_LATENCY",
    "MARKET_DATA_STALENESS",
    "ORDER_ERRORS_COUNTER",
    "WATCHDOG_HEALTH_GAUGE",
    "observe_api_request",
    "set_market_data_staleness",
    "record_order_error",
    "set_watchdog_health",
    "reset_for_tests",
]

API_REQUEST_LATENCY = Histogram(
    "api_request_latency_seconds",
    "Latency of HTTP API requests by route, method and status.",
    ("route", "method", "status"),
)

MARKET_DATA_STALENESS = Gauge(
    "market_data_staleness_seconds",
    "Age of the latest market data update by venue and symbol.",
    ("venue", "symbol"),
)

ORDER_ERRORS_COUNTER = Counter(
    "order_errors_total",
    "Total number of order errors by venue and reason.",
    ("venue", "reason"),
)

WATCHDOG_HEALTH_GAUGE = Gauge(
    "watchdog_health",
    "Exchange watchdog health indicator (1=healthy, 0=unhealthy).",
    ("venue",),
)


def _normalise_label(value: str | None) -> str:
    text = (value or "").strip().lower()
    return text or "unknown"


def observe_api_request(route: str, method: str, status: int, duration_seconds: float) -> None:
    """Record an HTTP API observation in the latency histogram."""

    route_label = (route or "").strip() or "/"
    method_label = (method or "").strip().upper() or "GET"
    status_label = str(int(status) if status is not None else 500)
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        duration = 0.0
    if duration < 0:
        duration = 0.0
    API_REQUEST_LATENCY.labels(route=route_label, method=method_label, status=status_label).observe(duration)


def set_market_data_staleness(venue: str, symbol: str, age_seconds: float) -> None:
    """Update the market data staleness gauge."""

    venue_label = _normalise_label(venue)
    symbol_label = (symbol or "").strip().upper() or "UNKNOWN"
    try:
        value = float(age_seconds)
    except (TypeError, ValueError):
        value = 0.0
    if value < 0:
        value = 0.0
    MARKET_DATA_STALENESS.labels(venue=venue_label, symbol=symbol_label).set(value)


def record_order_error(venue: str, reason: str | None) -> None:
    """Increment the order error counter for ``venue`` and ``reason``."""

    venue_label = _normalise_label(venue)
    reason_text = (reason or "").strip().lower()
    if not reason_text:
        reason_text = "generic"
    ORDER_ERRORS_COUNTER.labels(venue=venue_label, reason=reason_text).inc()


def set_watchdog_health(venue: str, healthy: bool) -> None:
    """Expose the watchdog health flag for ``venue``."""

    venue_label = _normalise_label(venue)
    WATCHDOG_HEALTH_GAUGE.labels(venue=venue_label).set(1.0 if healthy else 0.0)


def reset_for_tests() -> None:  # pragma: no cover - best effort cleanup
    """Reset dynamic metric state for deterministic tests."""

    collectors: Iterable = (
        API_REQUEST_LATENCY,
        MARKET_DATA_STALENESS,
        ORDER_ERRORS_COUNTER,
        WATCHDOG_HEALTH_GAUGE,
    )
    for metric in collectors:
        try:
            metric._metrics.clear()  # type: ignore[attr-defined]
        except Exception:
            pass
    for histogram in (API_REQUEST_LATENCY,):
        try:
            histogram._sum.set(0.0)  # type: ignore[attr-defined]
            histogram._count.set(0.0)  # type: ignore[attr-defined]
            for bucket in histogram._buckets:  # type: ignore[attr-defined]
                bucket.set(0.0)
        except Exception:
            pass
