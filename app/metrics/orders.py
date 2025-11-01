"""Prometheus metrics for idempotent order routing."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram


ORDER_INTENT_TOTAL = Counter(
    "order_intent_total",
    "Total order intents processed by state",
    labelnames=("state",),
)

IDEMPOTENCY_HIT_TOTAL = Counter(
    "order_idempotency_hit_total",
    "Number of duplicate requests detected",
    labelnames=("operation",),
)

OPEN_INTENTS_GAUGE = Gauge(
    "open_order_intents",
    "Number of order intents in non-terminal state",
)

REPLACE_CHAIN_LENGTH = Gauge(
    "order_replace_chain_length",
    "Current observed replacement chain length",
    labelnames=("intent_id",),
)

ORDER_SUBMIT_LATENCY = Histogram(
    "order_submit_latency_ms",
    "Latency of broker submissions",
    buckets=(5, 10, 25, 50, 100, 250, 500, 1000, 2000),
)


def record_open_intents(count: int) -> None:
    OPEN_INTENTS_GAUGE.set(float(count))


def observe_replace_chain(intent_id: str, length: int) -> None:
    REPLACE_CHAIN_LENGTH.labels(intent_id=intent_id).set(float(length))


__all__ = [
    "IDEMPOTENCY_HIT_TOTAL",
    "OPEN_INTENTS_GAUGE",
    "ORDER_INTENT_TOTAL",
    "ORDER_SUBMIT_LATENCY",
    "REPLACE_CHAIN_LENGTH",
    "observe_replace_chain",
    "record_open_intents",
]

