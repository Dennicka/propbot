"""Telemetry helpers for risk skips and related counters."""

from __future__ import annotations

from collections import defaultdict
from threading import Lock
from typing import Dict

from prometheus_client import Counter

_RISK_SKIP_COUNTER = Counter(
    "risk_skips_total",
    "Total number of risk-driven skip events",
    ("reason", "strategy"),
)

_lock = Lock()
_skip_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))


def _normalise_strategy(strategy: str | None) -> str:
    value = (strategy or "").strip()
    return value or "unknown"


def _normalise_reason(reason: str | None) -> str:
    value = (reason or "").strip()
    return value or "other_risk"


def record_risk_skip(strategy: str | None, reason: str | None) -> None:
    """Increment counters for a risk skip event."""

    strategy_name = _normalise_strategy(strategy)
    reason_code = _normalise_reason(reason)
    _RISK_SKIP_COUNTER.labels(reason=reason_code, strategy=strategy_name).inc()
    with _lock:
        _skip_counts[strategy_name][reason_code] += 1


def get_risk_skip_counts() -> Dict[str, Dict[str, int]]:
    """Return an in-memory snapshot of risk skip counters."""

    with _lock:
        return {
            strategy: dict(reason_counts)
            for strategy, reason_counts in _skip_counts.items()
        }


def reset_risk_skip_metrics_for_tests() -> None:  # pragma: no cover - test helper
    """Reset counters for deterministic tests."""

    with _lock:
        _skip_counts.clear()
    try:
        _RISK_SKIP_COUNTER._metrics.clear()  # type: ignore[attr-defined]
    except Exception:
        # Clearing is best-effort; individual sample values are reset via inc() tests.
        pass


__all__ = [
    "record_risk_skip",
    "get_risk_skip_counts",
    "reset_risk_skip_metrics_for_tests",
]

