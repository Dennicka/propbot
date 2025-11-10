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

STUCK_RESOLVER_RETRIES_TOTAL = Counter(
    "stuck_resolver_retries_total",
    "Number of retries performed by the stuck order resolver",
    labelnames=("venue", "symbol", "reason"),
)

STUCK_RESOLVER_FAILURES_TOTAL = Counter(
    "stuck_resolver_failures_total",
    "Number of stuck resolver attempts that resulted in a terminal failure",
    labelnames=("venue", "symbol", "reason"),
)

OPEN_ORDERS_GAUGE = Gauge(
    "open_orders_gauge",
    "Open orders grouped by venue, symbol, and status",
    labelnames=("venue", "symbol", "status"),
)

STUCK_RESOLVER_ACTIVE_INTENTS = Gauge(
    "stuck_resolver_active_intents",
    "Number of order intents currently monitored by the stuck resolver",
)


__all__ = [
    "ORDER_RETRIES_TOTAL",
    "OPEN_ORDERS_GAUGE",
    "STUCK_RESOLVER_ACTIVE_INTENTS",
    "STUCK_RESOLVER_FAILURES_TOTAL",
    "STUCK_RESOLVER_RETRIES_TOTAL",
    "STUCK_ORDERS_TOTAL",
]

