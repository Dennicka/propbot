"""Prometheus instrumentation for the UI cache."""

from __future__ import annotations

import threading
from typing import Dict, Tuple

from prometheus_client import Gauge

__all__ = [
    "record_cache_observation",
    "reset_cache_metrics",
]

_CACHE_HIT_RATIO: Gauge | None = None
_CACHE_LOCK = threading.Lock()
_CACHE_COUNTS: Dict[str, Tuple[int, int]] = {}


def _get_gauge() -> Gauge:
    global _CACHE_HIT_RATIO
    if _CACHE_HIT_RATIO is None:
        _CACHE_HIT_RATIO = Gauge(
            "cache_hit_ratio",
            "Hit ratio of the in-process UI cache by endpoint.",
            ("endpoint",),
        )
    return _CACHE_HIT_RATIO


def record_cache_observation(endpoint: str, hit: bool) -> None:
    """Update hit ratio metrics for ``endpoint``."""

    gauge = _get_gauge()
    with _CACHE_LOCK:
        hits, total = _CACHE_COUNTS.get(endpoint, (0, 0))
        total += 1
        if hit:
            hits += 1
        _CACHE_COUNTS[endpoint] = (hits, total)
        ratio = hits / total if total else 0.0
    gauge.labels(endpoint=endpoint).set(ratio)


def reset_cache_metrics() -> None:
    """Reset cached counters and gauges (used in tests)."""

    global _CACHE_COUNTS
    with _CACHE_LOCK:
        _CACHE_COUNTS = {}
        if _CACHE_HIT_RATIO is not None:
            _CACHE_HIT_RATIO.clear()
