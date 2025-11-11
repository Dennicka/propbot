"""Prometheus collectors for the risk governor."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

# ---------------------------------------------------------------------------
# Sliding-window governor metrics (legacy)
# ---------------------------------------------------------------------------
_SUCCESS_RATE_GAUGE = Gauge(
    "propbot_risk_success_rate_1h",
    "Rolling 1h order success rate observed by the risk governor.",
)
_SUCCESS_RATE_GAUGE.set(1.0)

_ERROR_RATE_GAUGE = Gauge(
    "propbot_risk_order_error_rate_1h",
    "Rolling 1h order error rate observed by the risk governor.",
)
_ERROR_RATE_GAUGE.set(0.0)

_THROTTLED_GAUGE = Gauge(
    "propbot_risk_throttled",
    "Current throttle state exposed by the risk governor (1 when active).",
    ("reason",),
)
_THROTTLED_GAUGE.labels(reason="none").set(0.0)

_WINDOWS_COUNTER = Counter(
    "propbot_risk_windows_total",
    "Total number of evaluated risk windows, labelled by throttle state.",
    ("throttled",),
)
_WINDOWS_COUNTER.labels(throttled="false").inc(0.0)
_WINDOWS_COUNTER.labels(throttled="true").inc(0.0)

# ---------------------------------------------------------------------------
# Risk-governor v2 metrics
# ---------------------------------------------------------------------------
_RISK_CHECKS_COUNTER = Counter(
    "propbot_risk_checks_total",
    "Total number of pre-trade risk checks, labelled by result and reason.",
    ("result", "reason"),
)
_ORDERS_BLOCKED_COUNTER = Counter(
    "propbot_orders_blocked_total",
    "Number of orders blocked by the risk governor, labelled by reason.",
    ("reason",),
)
_VELOCITY_GAUGE = Gauge(
    "propbot_risk_velocity_window",
    "Aggregated order velocity observed by the risk governor.",
    ("kind",),
)


def set_success_rate(value: float) -> None:
    """Expose the rolling success rate to Prometheus."""

    try:
        _SUCCESS_RATE_GAUGE.set(float(value))
    except (TypeError, ValueError):
        _SUCCESS_RATE_GAUGE.set(0.0)


def set_error_rate(value: float) -> None:
    """Expose the rolling error rate to Prometheus."""

    try:
        _ERROR_RATE_GAUGE.set(float(value))
    except (TypeError, ValueError):
        _ERROR_RATE_GAUGE.set(0.0)


def set_throttled(active: bool, reason: str | None) -> None:
    """Update the risk throttle gauge for the active reason."""

    reason_label = (reason or "none").strip().lower() or "none"
    _THROTTLED_GAUGE.labels(reason=reason_label).set(1.0 if active else 0.0)


def increment_window(throttled: bool) -> None:
    """Increment the evaluated window counter."""

    _WINDOWS_COUNTER.labels(throttled="true" if throttled else "false").inc()


def record_risk_check(result: str, reason: str | None = None) -> None:
    """Record the outcome of a pre-trade risk check."""

    result_label = (result or "unknown").strip().lower() or "unknown"
    reason_label = (reason or "none").strip().lower() or "none"
    _RISK_CHECKS_COUNTER.labels(result=result_label, reason=reason_label).inc()


def record_blocked_order(reason: str | None = None) -> None:
    """Increment the counter for a blocked order."""

    reason_label = (reason or "none").strip().lower() or "none"
    _ORDERS_BLOCKED_COUNTER.labels(reason=reason_label).inc()


def set_velocity(kind: str, value: float) -> None:
    """Expose velocity window aggregates for the given kind."""

    label = (kind or "unknown").strip().lower() or "unknown"
    try:
        _VELOCITY_GAUGE.labels(kind=label).set(float(value))
    except (TypeError, ValueError):
        _VELOCITY_GAUGE.labels(kind=label).set(0.0)


__all__ = [
    "set_success_rate",
    "set_error_rate",
    "set_throttled",
    "increment_window",
    "record_risk_check",
    "record_blocked_order",
    "set_velocity",
]
