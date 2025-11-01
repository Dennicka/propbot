from __future__ import annotations

"""Prometheus collectors for the risk governor sliding window."""

from prometheus_client import Counter, Gauge

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


def set_success_rate(value: float) -> None:
    """Expose the rolling success rate to Prometheus."""

    try:
        _SUCCESS_RATE_GAUGE.set(float(value))
    except Exception:
        _SUCCESS_RATE_GAUGE.set(0.0)


def set_error_rate(value: float) -> None:
    """Expose the rolling error rate to Prometheus."""

    try:
        _ERROR_RATE_GAUGE.set(float(value))
    except Exception:
        _ERROR_RATE_GAUGE.set(0.0)


def set_throttled(active: bool, reason: str | None) -> None:
    """Update the risk throttle gauge for the active reason."""

    reason_label = (reason or "none").strip().lower() or "none"
    _THROTTLED_GAUGE.labels(reason=reason_label).set(1.0 if active else 0.0)


def increment_window(throttled: bool) -> None:
    """Increment the evaluated window counter."""

    _WINDOWS_COUNTER.labels(throttled="true" if throttled else "false").inc()


__all__ = [
    "set_success_rate",
    "set_error_rate",
    "set_throttled",
    "increment_window",
]
