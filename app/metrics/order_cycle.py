"""Prometheus metrics for order lifecycle latency observations."""

from __future__ import annotations

import logging

from prometheus_client import Histogram

LOGGER = logging.getLogger(__name__)

ORDER_CYCLE_SECONDS = Histogram(
    "order_cycle_seconds",
    "Order lifecycle duration from initial submit to terminal state",
    labelnames=("runtime_profile", "venue", "outcome"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)


def observe_order_cycle(
    *,
    runtime_profile: str,
    venue: str,
    outcome: str,
    seconds: float,
) -> None:
    """Record a single order lifecycle observation."""

    try:
        ORDER_CYCLE_SECONDS.labels(
            runtime_profile=runtime_profile,
            venue=venue,
            outcome=outcome,
        ).observe(seconds)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.warning(
            "Failed to observe order cycle metric for venue %s: %s",
            venue,
            exc,
        )


__all__ = [
    "ORDER_CYCLE_SECONDS",
    "observe_order_cycle",
]
