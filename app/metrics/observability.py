"""Prometheus metrics underpinning SLOs and alerting rules."""

from __future__ import annotations

import os
import threading
from typing import Iterable

from prometheus_client import Counter, Gauge, Histogram

__all__ = [
    "API_REQUEST_LATENCY",
    "MARKET_DATA_STALENESS",
    "ORDER_ERRORS_COUNTER",
    "WATCHDOG_HEALTH_GAUGE",
    "METRICS_SLO_ENABLED",
    "observe_api_request",
    "set_market_data_staleness",
    "record_order_error",
    "set_watchdog_health",
    "register_slo_metrics",
    "reset_for_tests",
]


def _env_flag(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


METRICS_SLO_ENABLED: bool = _env_flag(os.getenv("METRICS_SLO_ENABLED"), False)

API_REQUEST_LATENCY: Histogram | None = None
MARKET_DATA_STALENESS: Gauge | None = None
ORDER_ERRORS_COUNTER: Counter | None = None
WATCHDOG_HEALTH_GAUGE: Gauge | None = None

_REGISTRATION_LOCK = threading.Lock()


def register_slo_metrics() -> bool:
    """Ensure the SLO metrics are registered exactly once when enabled."""

    global API_REQUEST_LATENCY
    global MARKET_DATA_STALENESS
    global ORDER_ERRORS_COUNTER
    global WATCHDOG_HEALTH_GAUGE

    if not METRICS_SLO_ENABLED:
        return False
    if (
        API_REQUEST_LATENCY is not None
        and MARKET_DATA_STALENESS is not None
        and ORDER_ERRORS_COUNTER is not None
        and WATCHDOG_HEALTH_GAUGE is not None
    ):
        return True

    with _REGISTRATION_LOCK:
        if API_REQUEST_LATENCY is None:
            API_REQUEST_LATENCY = Histogram(
                "api_request_latency_seconds",
                "Latency of HTTP API requests by route, method and status.",
                ("route", "method", "status"),
            )
        if MARKET_DATA_STALENESS is None:
            MARKET_DATA_STALENESS = Gauge(
                "market_data_staleness_seconds",
                "Age of the latest market data update by venue and symbol.",
                ("venue", "symbol"),
            )
        if ORDER_ERRORS_COUNTER is None:
            ORDER_ERRORS_COUNTER = Counter(
                "order_errors_total",
                "Total number of order errors by venue and reason.",
                ("venue", "reason"),
            )
        if WATCHDOG_HEALTH_GAUGE is None:
            WATCHDOG_HEALTH_GAUGE = Gauge(
                "watchdog_health",
                "Exchange watchdog health indicator (1=healthy, 0=unhealthy).",
                ("venue",),
            )
    return True


def _normalise_label(value: str | None) -> str:
    text = (value or "").strip().lower()
    return text or "unknown"


def observe_api_request(route: str, method: str, status: int, duration_seconds: float) -> None:
    """Record an HTTP API observation in the latency histogram."""

    if not register_slo_metrics():
        return
    assert API_REQUEST_LATENCY is not None
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

    if not register_slo_metrics():
        return
    assert MARKET_DATA_STALENESS is not None
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

    if not register_slo_metrics():
        return
    assert ORDER_ERRORS_COUNTER is not None
    venue_label = _normalise_label(venue)
    reason_text = (reason or "").strip().lower()
    if not reason_text:
        reason_text = "generic"
    ORDER_ERRORS_COUNTER.labels(venue=venue_label, reason=reason_text).inc()


def set_watchdog_health(venue: str, healthy: bool) -> None:
    """Expose the watchdog health flag for ``venue``."""

    if not register_slo_metrics():
        return
    assert WATCHDOG_HEALTH_GAUGE is not None
    venue_label = _normalise_label(venue)
    WATCHDOG_HEALTH_GAUGE.labels(venue=venue_label).set(1.0 if healthy else 0.0)


def reset_for_tests() -> None:  # pragma: no cover - best effort cleanup
    """Reset dynamic metric state for deterministic tests."""

    collectors: Iterable = tuple(
        metric
        for metric in (
            API_REQUEST_LATENCY,
            MARKET_DATA_STALENESS,
            ORDER_ERRORS_COUNTER,
            WATCHDOG_HEALTH_GAUGE,
        )
        if metric is not None
    )
    for metric in collectors:
        try:
            metric._metrics.clear()  # type: ignore[attr-defined]
        except Exception:
            pass
    if API_REQUEST_LATENCY is not None:
        try:
            API_REQUEST_LATENCY._sum.set(0.0)  # type: ignore[attr-defined]
            API_REQUEST_LATENCY._count.set(0.0)  # type: ignore[attr-defined]
            for bucket in API_REQUEST_LATENCY._buckets:  # type: ignore[attr-defined]
                bucket.set(0.0)
        except Exception:
            pass
