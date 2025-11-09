"""Execution-related Prometheus metrics for stuck order resolution."""

from __future__ import annotations

from prometheus_client import Counter, Gauge


STUCK_ORDERS_TOTAL = Counter(
    "stuck_orders_total",
    "Number of stuck orders detected by the resolver",
    labelnames=("venue", "symbol"),
)

ORDER_RETRIES_TOTAL = Counter(
    "order_retries_total",
    "Number of retries issued for stuck orders",
    labelnames=("venue", "symbol"),
)

OPEN_ORDERS_GAUGE = Gauge(
    "open_orders_gauge",
    "Open orders grouped by venue, symbol, and status",
    labelnames=("venue", "symbol", "status"),
)


__all__ = [
    "ORDER_RETRIES_TOTAL",
    "OPEN_ORDERS_GAUGE",
    "STUCK_ORDERS_TOTAL",
]

