"""Lightweight Prometheus metrics registry with atomic exporter."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, Mapping, MutableMapping, Sequence

DEFAULT_METRICS_PATH = "data/metrics/metrics.prom"
METRICS_PATH_ENV = "METRICS_PATH"
METRICS_BUCKETS_ENV = "METRICS_BUCKETS_MS"
DEFAULT_BUCKETS_MS: list[float] = [
    5.0,
    10.0,
    20.0,
    50.0,
    100.0,
    200.0,
    500.0,
    1000.0,
    2000.0,
]


def _parse_buckets(raw: str | None) -> list[float]:
    if not raw:
        return list(DEFAULT_BUCKETS_MS)
    buckets: list[float] = []
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            value = float(token)
        except ValueError:
            continue
        buckets.append(value)
    if not buckets:
        return list(DEFAULT_BUCKETS_MS)
    buckets.sort()
    return buckets


def _escape_label_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace('"', '\\"')
    )


def _format_value(value: float) -> str:
    return format(value, "g")


class CounterChild:
    def __init__(self, parent: Counter, label_values: tuple[str, ...]):
        self._parent = parent
        self._label_values = label_values

    def inc(self, value: float = 1.0) -> None:
        self._parent._inc(self._label_values, float(value))


class GaugeChild:
    def __init__(self, parent: Gauge, label_values: tuple[str, ...]):
        self._parent = parent
        self._label_values = label_values

    def set(self, value: float) -> None:
        self._parent._set(self._label_values, float(value))

    def inc(self, value: float = 1.0) -> None:
        self._parent._inc(self._label_values, float(value))

    def dec(self, value: float = 1.0) -> None:
        self._parent._inc(self._label_values, -float(value))


class HistogramChild:
    def __init__(self, parent: Histogram, label_values: tuple[str, ...]):
        self._parent = parent
        self._label_values = label_values

    def observe(self, value: float) -> None:
        self._parent._observe(self._label_values, float(value))


class Counter:
    __slots__ = ("name", "label_names", "_values", "_lock")

    def __init__(self, name: str, labels: Sequence[str] = ()) -> None:
        self.name = name
        self.label_names: tuple[str, ...] = tuple(labels)
        self._values: Dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def _normalize_labels(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        if len(labels) != len(self.label_names):
            missing = [name for name in self.label_names if name not in labels]
            unexpected = [name for name in labels if name not in self.label_names]
            raise ValueError(
                f"invalid labels for {self.name}: missing={missing} unexpected={unexpected}"
            )
        return tuple(str(labels[name]) for name in self.label_names)

    def labels(self, **labels: str) -> CounterChild:
        label_values = self._normalize_labels(labels)
        with self._lock:
            if label_values not in self._values:
                self._values[label_values] = 0.0
            return CounterChild(self, label_values)

    def inc(self, value: float = 1.0) -> None:
        self._inc((), float(value))

    def _inc(self, label_values: tuple[str, ...], value: float) -> None:
        if value == 0.0:
            return
        with self._lock:
            current = self._values.get(label_values, 0.0)
            self._values[label_values] = current + value

    def render(self) -> list[str]:
        lines = [f"# TYPE {self.name} counter"]
        with self._lock:
            items = sorted(self._values.items(), key=lambda item: item[0])
        for label_values, value in items:
            if label_values:
                lines.append(_format_metric(self.name, self.label_names, label_values, value))
            else:
                lines.append(f"{self.name} {_format_value(value)}")
        return lines


class Gauge:
    __slots__ = ("name", "label_names", "_values", "_lock")

    def __init__(self, name: str, labels: Sequence[str] = ()) -> None:
        self.name = name
        self.label_names: tuple[str, ...] = tuple(labels)
        self._values: Dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def _normalize_labels(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        if len(labels) != len(self.label_names):
            missing = [name for name in self.label_names if name not in labels]
            unexpected = [name for name in labels if name not in self.label_names]
            raise ValueError(
                f"invalid labels for {self.name}: missing={missing} unexpected={unexpected}"
            )
        return tuple(str(labels[name]) for name in self.label_names)

    def labels(self, **labels: str) -> GaugeChild:
        label_values = self._normalize_labels(labels)
        with self._lock:
            if label_values not in self._values:
                self._values[label_values] = 0.0
            return GaugeChild(self, label_values)

    def set(self, value: float) -> None:
        self._set((), float(value))

    def _set(self, label_values: tuple[str, ...], value: float) -> None:
        with self._lock:
            self._values[label_values] = value

    def inc(self, value: float = 1.0) -> None:
        self._inc((), float(value))

    def dec(self, value: float = 1.0) -> None:
        self._inc((), -float(value))

    def _inc(self, label_values: tuple[str, ...], value: float) -> None:
        if value == 0.0:
            return
        with self._lock:
            current = self._values.get(label_values, 0.0)
            self._values[label_values] = current + value

    def render(self) -> list[str]:
        lines = [f"# TYPE {self.name} gauge"]
        with self._lock:
            items = sorted(self._values.items(), key=lambda item: item[0])
        for label_values, value in items:
            if label_values:
                lines.append(_format_metric(self.name, self.label_names, label_values, value))
            else:
                lines.append(f"{self.name} {_format_value(value)}")
        return lines


class Histogram:
    __slots__ = ("name", "label_names", "buckets", "_values", "_lock")

    def __init__(self, name: str, buckets: Sequence[float], labels: Sequence[str] = ()) -> None:
        self.name = name
        self.label_names: tuple[str, ...] = tuple(labels)
        ordered = sorted(float(bucket) for bucket in buckets)
        self.buckets: tuple[float, ...] = tuple(ordered)
        self._values: Dict[tuple[str, ...], _HistogramValue] = {}
        self._lock = threading.Lock()

    def _normalize_labels(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        if len(labels) != len(self.label_names):
            missing = [name for name in self.label_names if name not in labels]
            unexpected = [name for name in labels if name not in self.label_names]
            raise ValueError(
                f"invalid labels for {self.name}: missing={missing} unexpected={unexpected}"
            )
        return tuple(str(labels[name]) for name in self.label_names)

    def labels(self, **labels: str) -> HistogramChild:
        label_values = self._normalize_labels(labels)
        with self._lock:
            if label_values not in self._values:
                self._values[label_values] = _HistogramValue(len(self.buckets))
            return HistogramChild(self, label_values)

    def observe(self, value: float) -> None:
        self._observe((), float(value))

    def _observe(self, label_values: tuple[str, ...], value: float) -> None:
        with self._lock:
            hist = self._values.get(label_values)
            if hist is None:
                hist = _HistogramValue(len(self.buckets))
                self._values[label_values] = hist
            hist.observe(value, self.buckets)

    def render(self) -> list[str]:
        lines = [f"# TYPE {self.name} histogram"]
        with self._lock:
            snapshot = [(labels, hist.snapshot()) for labels, hist in self._values.items()]
            buckets = self.buckets
        for label_values, (counts, total_count, total_sum) in sorted(
            snapshot, key=lambda item: item[0]
        ):
            base_labels = self.label_names if label_values else ()
            for bucket, count in zip(buckets, counts):
                extra = {"le": _format_value(bucket)}
                lines.append(
                    _format_metric(
                        f"{self.name}_bucket",
                        base_labels,
                        label_values,
                        count,
                        extra=extra,
                    )
                )
            lines.append(
                _format_metric(
                    f"{self.name}_bucket",
                    base_labels,
                    label_values,
                    total_count,
                    extra={"le": "+Inf"},
                )
            )
            lines.append(
                _format_metric(
                    f"{self.name}_sum",
                    base_labels,
                    label_values,
                    total_sum,
                )
            )
            lines.append(
                _format_metric(
                    f"{self.name}_count",
                    base_labels,
                    label_values,
                    total_count,
                )
            )
        return lines


class _HistogramValue:
    __slots__ = ("counts", "count", "sum")

    def __init__(self, size: int) -> None:
        self.counts = [0.0] * size
        self.count = 0.0
        self.sum = 0.0

    def observe(self, value: float, buckets: Sequence[float]) -> None:
        self.count += 1.0
        self.sum += value
        for idx, upper in enumerate(buckets):
            if value <= upper:
                self.counts[idx] += 1.0

    def snapshot(self) -> tuple[list[float], float, float]:
        return (self.counts.copy(), self.count, self.sum)


def _format_metric(
    name: str,
    label_names: Sequence[str],
    label_values: Sequence[str],
    value: float,
    *,
    extra: Mapping[str, str] | None = None,
) -> str:
    if not label_names and not extra:
        return f"{name} {_format_value(value)}"
    labels: list[str] = []
    for key, current in zip(label_names, label_values):
        labels.append(f'{key}="{_escape_label_value(current)}"')
    if extra:
        for key, current in extra.items():
            labels.append(f'{key}="{_escape_label_value(current)}"')
    return f"{name}{{{','.join(labels)}}} {_format_value(value)}"


class Registry:
    def __init__(self) -> None:
        self._metrics: MutableMapping[str, object] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def _register(self, name: str, metric: object) -> object:
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = metric
                self._order.append(name)
            return self._metrics[name]

    def counter(self, name: str, labels: Sequence[str] = ()) -> Counter:
        metric = self._metrics.get(name)
        if metric is not None:
            if not isinstance(metric, Counter):
                raise TypeError(f"metric {name} already registered with different type")
            if metric.label_names != tuple(labels):
                raise ValueError(
                    f"metric {name} already registered with labels {metric.label_names}"
                )
            return metric
        counter = Counter(name, labels)
        return self._register(name, counter)  # type: ignore[return-value]

    def gauge(self, name: str, labels: Sequence[str] = ()) -> Gauge:
        metric = self._metrics.get(name)
        if metric is not None:
            if not isinstance(metric, Gauge):
                raise TypeError(f"metric {name} already registered with different type")
            if metric.label_names != tuple(labels):
                raise ValueError(
                    f"metric {name} already registered with labels {metric.label_names}"
                )
            return metric
        gauge = Gauge(name, labels)
        return self._register(name, gauge)  # type: ignore[return-value]

    def histogram(
        self, name: str, buckets: Sequence[float], labels: Sequence[str] = ()
    ) -> Histogram:
        metric = self._metrics.get(name)
        if metric is not None:
            if not isinstance(metric, Histogram):
                raise TypeError(f"metric {name} already registered with different type")
            if metric.label_names != tuple(labels):
                raise ValueError(
                    f"metric {name} already registered with labels {metric.label_names}"
                )
            return metric
        histogram = Histogram(name, buckets, labels)
        return self._register(name, histogram)  # type: ignore[return-value]

    def to_text(self) -> str:
        lines: list[str] = []
        for name in list(self._order):
            metric = self._metrics.get(name)
            if metric is None:
                continue
            if hasattr(metric, "render"):
                lines.extend(metric.render())  # type: ignore[arg-type]
        if not lines:
            return ""
        return "\n".join(lines) + "\n"


_REGISTRY: Registry | None = None
_DEFAULT_BUCKETS = _parse_buckets(os.getenv(METRICS_BUCKETS_ENV))


def get_registry() -> Registry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = Registry()
    return _REGISTRY


def counter(name: str, labels: Sequence[str] = ()) -> Counter:
    return get_registry().counter(name, labels)


def gauge(name: str, labels: Sequence[str] = ()) -> Gauge:
    return get_registry().gauge(name, labels)


def histogram(
    name: str, *, buckets: Sequence[float] | None = None, labels: Sequence[str] = ()
) -> Histogram:
    bucket_list = list(buckets) if buckets is not None else list(_DEFAULT_BUCKETS)
    return get_registry().histogram(name, bucket_list, labels)


HEALTH_WATCHDOG_OVERALL_GAUGE = gauge("propbot_health_watchdog_overall", labels=("level",))
for level in ("ok", "warn", "fail"):
    HEALTH_WATCHDOG_OVERALL_GAUGE.labels(level=level).set(0.0)

HEALTH_WATCHDOG_COMPONENT_GAUGE = gauge(
    "propbot_health_watchdog_component_level", labels=("component", "level")
)
for level in ("ok", "warn", "fail"):
    HEALTH_WATCHDOG_COMPONENT_GAUGE.labels(component="unknown", level=level).set(0.0)


def write_metrics(path: str | os.PathLike[str] | None = None) -> None:
    payload = get_registry().to_text()
    target = Path(path or os.getenv(METRICS_PATH_ENV, DEFAULT_METRICS_PATH))
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent), delete=False
    ) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        tmp_name = handle.name
    os.replace(tmp_name, target)


__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "Registry",
    "DEFAULT_METRICS_PATH",
    "METRICS_PATH_ENV",
    "METRICS_BUCKETS_ENV",
    "counter",
    "gauge",
    "get_registry",
    "histogram",
    "write_metrics",
    "HEALTH_WATCHDOG_COMPONENT_GAUGE",
    "HEALTH_WATCHDOG_OVERALL_GAUGE",
]
