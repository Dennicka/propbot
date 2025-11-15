"""Prometheus metrics for smart order router decisions."""

from prometheus_client import Counter

sor_decisions_total = Counter(
    "sor_decisions_total",
    "Total number of SOR routing decisions",
    labelnames=("strategy_id", "venue_id", "result"),
)

__all__ = ["sor_decisions_total"]
