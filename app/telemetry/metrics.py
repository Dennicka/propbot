from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable

from prometheus_client import Counter, Gauge, Histogram

from ..metrics.slo import WATCHDOG_OK_GAUGE

__all__ = [
    "UI_LATENCY",
    "CORE_OPERATION_LATENCY",
    "ERROR_COUNTER",
    "WATCHDOG_OK_GAUGE",
    "SCANNER_OK_GAUGE",
    "HEDGE_DAEMON_OK_GAUGE",
    "observe_ui_latency",
    "observe_core_latency",
    "record_error",
    "set_watchdog_ok",
    "set_scanner_ok",
    "set_hedge_daemon_ok",
    "slo_snapshot",
]


@dataclass
class _Stats:
    samples: Deque[float]
    total: int = 0
    errors: int = 0

    def record(self, value: float, *, error: bool) -> None:
        self.total += 1
        if error:
            self.errors += 1
        self.samples.append(max(0.0, value))

    def percentile(self, pct: float) -> float | None:
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        rank = pct / 100.0 * (len(ordered) - 1)
        lower = int(math.floor(rank))
        upper = int(math.ceil(rank))
        if lower == upper:
            return ordered[lower]
        fraction = rank - lower
        return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction

    @property
    def error_rate(self) -> float:
        if self.total <= 0:
            return 0.0
        return self.errors / max(1, self.total)


_MAX_SAMPLES = 512
_STATS_LOCK = threading.Lock()
_UI_STATS = _Stats(deque(maxlen=_MAX_SAMPLES))
_CORE_STATS: Dict[str, _Stats] = {
    "scan": _Stats(deque(maxlen=_MAX_SAMPLES)),
    "hedge": _Stats(deque(maxlen=_MAX_SAMPLES)),
}

UI_LATENCY = Histogram(
    "propbot_ui_latency_ms",
    "Latency of operator UI HTTP requests",
    ("endpoint",),
    buckets=(5, 10, 25, 50, 75, 100, 200, 400, 800, 1600),
)
CORE_OPERATION_LATENCY = Histogram(
    "propbot_core_operation_latency_ms",
    "Latency of internal scanning and hedging operations",
    ("operation",),
    buckets=(10, 25, 50, 100, 250, 500, 1000, 2000, 5000),
)
ERROR_COUNTER = Counter(
    "propbot_error_total",
    "Number of errors encountered by context",
    ("context",),
)
SCANNER_OK_GAUGE = Gauge(
    "propbot_scanner_ok",
    "Opportunity scanner health indicator",
)
HEDGE_DAEMON_OK_GAUGE = Gauge(
    "propbot_hedge_daemon_ok",
    "Auto hedge daemon health indicator",
)

for context in ("ui", "scanner", "hedge"):
    ERROR_COUNTER.labels(context=context).inc(0.0)
SCANNER_OK_GAUGE.set(1.0)
HEDGE_DAEMON_OK_GAUGE.set(1.0)


def _normalise_endpoint(path: str) -> str:
    cleaned = str(path or "").split("?", 1)[0]
    if not cleaned:
        return "/"
    segments = [segment for segment in cleaned.split("/") if segment]
    if not segments:
        return "/"
    prefix = ["", *segments[:3]]
    return "/".join(prefix)


def _coerce_duration(value: float | int | None) -> float:
    if value is None:
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):  # pragma: no cover - defensive path
        return 0.0


def _core_key(operation: str) -> str:
    label = str(operation or "").strip().lower() or "unknown"
    with _STATS_LOCK:
        if label not in _CORE_STATS:
            _CORE_STATS[label] = _Stats(deque(maxlen=_MAX_SAMPLES))
    return label


def observe_ui_latency(
    path: str, duration_ms: float, *, status_code: int | None = None, error: bool = False
) -> None:
    value = _coerce_duration(duration_ms)
    endpoint = _normalise_endpoint(path)
    UI_LATENCY.labels(endpoint=endpoint).observe(value)
    failure = error or (status_code is not None and status_code >= 500)
    with _STATS_LOCK:
        _UI_STATS.record(value, error=failure)
    if failure:
        record_error("ui")


def observe_core_latency(operation: str, duration_ms: float, *, error: bool = False) -> None:
    value = _coerce_duration(duration_ms)
    key = _core_key(operation)
    CORE_OPERATION_LATENCY.labels(operation=key).observe(value)
    with _STATS_LOCK:
        _CORE_STATS[key].record(value, error=error)
    if error:
        record_error(key)


def record_error(context: str) -> None:
    label = str(context or "unknown").strip().lower() or "unknown"
    ERROR_COUNTER.labels(context=label).inc()


def set_watchdog_ok(exchange: str | None, ok: bool) -> None:
    label = (exchange or "unknown").strip() or "unknown"
    WATCHDOG_OK_GAUGE.labels(exchange=label).set(1.0 if ok else 0.0)


def set_scanner_ok(ok: bool) -> None:
    SCANNER_OK_GAUGE.set(1.0 if ok else 0.0)


def set_hedge_daemon_ok(ok: bool) -> None:
    HEDGE_DAEMON_OK_GAUGE.set(1.0 if ok else 0.0)


def reset_for_tests() -> None:  # pragma: no cover - test utility
    """Reset in-memory telemetry accumulators for deterministic tests."""

    with _STATS_LOCK:
        _UI_STATS.samples.clear()
        _UI_STATS.total = 0
        _UI_STATS.errors = 0
        for stats in _CORE_STATS.values():
            stats.samples.clear()
            stats.total = 0
            stats.errors = 0
    SCANNER_OK_GAUGE.set(1.0)
    HEDGE_DAEMON_OK_GAUGE.set(1.0)


def _stats_payload(stats: _Stats) -> Dict[str, float | int | None]:
    return {
        "p95_ms": stats.percentile(95.0),
        "count": stats.total,
        "errors": stats.errors,
        "error_rate": stats.error_rate,
    }


def slo_snapshot() -> Dict[str, object]:
    with _STATS_LOCK:
        ui_snapshot = _stats_payload(_UI_STATS)
        core_snapshot = {name: _stats_payload(stats) for name, stats in _CORE_STATS.items()}
    totals: Iterable[_Stats] = [_UI_STATS, *_CORE_STATS.values()]
    overall_total = sum(stats.total for stats in totals)
    overall_errors = sum(stats.errors for stats in totals)
    overall_rate = overall_errors / overall_total if overall_total else 0.0
    return {
        "ui": ui_snapshot,
        "core": core_snapshot,
        "overall": {
            "total": overall_total,
            "errors": overall_errors,
            "error_rate": overall_rate,
        },
    }
