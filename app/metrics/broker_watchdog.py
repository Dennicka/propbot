"""Prometheus metrics for the broker watchdog."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

_STATE_LABELS = ("OK", "DEGRADED", "DOWN")

WS_LAG_GAUGE = Gauge(
    "propbot_watchdog_ws_lag_ms_p95",
    "Websocket lag P95 observed by the broker watchdog",
    ("venue",),
)
WS_LAG_GAUGE.labels(venue="unknown").set(0.0)

WS_DISCONNECT_COUNTER = Counter(
    "propbot_watchdog_ws_disconnects_total",
    "Total websocket disconnects observed per venue",
    ("venue",),
)
WS_DISCONNECT_COUNTER.labels(venue="unknown").inc(0.0)

REST_5XX_GAUGE = Gauge(
    "propbot_watchdog_rest_5xx_rate",
    "Rate of REST 5xx responses over the rolling window",
    ("venue",),
)
REST_5XX_GAUGE.labels(venue="unknown").set(0.0)

REST_TIMEOUT_GAUGE = Gauge(
    "propbot_watchdog_rest_timeout_rate",
    "Rate of REST timeouts over the rolling window",
    ("venue",),
)
REST_TIMEOUT_GAUGE.labels(venue="unknown").set(0.0)

ORDER_REJECT_RATE_GAUGE = Gauge(
    "propbot_watchdog_order_reject_rate",
    "Rate of order rejections over the rolling window",
    ("venue",),
)
ORDER_REJECT_RATE_GAUGE.labels(venue="unknown").set(0.0)

AUTO_HOLD_COUNTER = Counter(
    "propbot_watchdog_auto_hold_total",
    "Number of auto-hold engagements triggered by the broker watchdog",
    ("venue",),
)
AUTO_HOLD_COUNTER.labels(venue="unknown").inc(0.0)

WATCHDOG_STATE_GAUGE = Gauge(
    "propbot_broker_watchdog_state",
    "Broker watchdog state indicator (1 when the labelled state is active)",
    ("venue", "state"),
)
for state in _STATE_LABELS:
    WATCHDOG_STATE_GAUGE.labels(venue="unknown", state=state).set(0.0)


def update_metrics(venue: str, *, ws_lag_ms_p95: float, rest_5xx_rate: float, rest_timeouts_rate: float, order_reject_rate: float) -> None:
    label = venue or "unknown"
    WS_LAG_GAUGE.labels(venue=label).set(float(ws_lag_ms_p95))
    REST_5XX_GAUGE.labels(venue=label).set(float(rest_5xx_rate))
    REST_TIMEOUT_GAUGE.labels(venue=label).set(float(rest_timeouts_rate))
    ORDER_REJECT_RATE_GAUGE.labels(venue=label).set(float(order_reject_rate))


def increment_disconnect(venue: str) -> None:
    WS_DISCONNECT_COUNTER.labels(venue=venue or "unknown").inc()


def record_auto_hold(venue: str) -> None:
    AUTO_HOLD_COUNTER.labels(venue=venue or "unknown").inc()


def set_state(venue: str, state: str) -> None:
    label = venue or "unknown"
    state_label = (state or "").strip().upper()
    for candidate in _STATE_LABELS:
        WATCHDOG_STATE_GAUGE.labels(venue=label, state=candidate).set(
            1.0 if candidate == state_label else 0.0
        )


__all__ = [
    "WS_LAG_GAUGE",
    "WS_DISCONNECT_COUNTER",
    "REST_5XX_GAUGE",
    "REST_TIMEOUT_GAUGE",
    "ORDER_REJECT_RATE_GAUGE",
    "AUTO_HOLD_COUNTER",
    "WATCHDOG_STATE_GAUGE",
    "update_metrics",
    "increment_disconnect",
    "record_auto_hold",
    "set_state",
]
