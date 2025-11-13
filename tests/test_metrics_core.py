"""Unit tests for the lightweight Prometheus registry."""

from __future__ import annotations

from pathlib import Path

import app.metrics.core as core


def test_registry_prometheus_text() -> None:
    registry = core.Registry()

    counter = registry.counter("demo_counter_total", labels=("label",))
    counter.labels(label="alpha").inc()
    counter.labels(label="alpha").inc(2)

    gauge = registry.gauge("demo_gauge", labels=("status",))
    gauge.labels(status="ok").set(3.5)

    histogram = registry.histogram(
        "demo_latency_ms",
        buckets=[1.0, 5.0],
        labels=("route",),
    )
    child = histogram.labels(route="primary")
    child.observe(0.5)
    child.observe(3.0)

    payload = registry.to_text()

    assert "# TYPE demo_counter_total counter" in payload
    assert 'demo_counter_total{label="alpha"} 3' in payload
    assert "# TYPE demo_gauge gauge" in payload
    assert 'demo_gauge{status="ok"} 3.5' in payload
    assert "# TYPE demo_latency_ms histogram" in payload
    assert 'demo_latency_ms_bucket{route="primary",le="1"} 1' in payload
    assert 'demo_latency_ms_bucket{route="primary",le="5"} 2' in payload
    assert 'demo_latency_ms_bucket{route="primary",le="+Inf"} 2' in payload
    assert 'demo_latency_ms_sum{route="primary"} 3.5' in payload
    assert 'demo_latency_ms_count{route="primary"} 2' in payload


def test_write_metrics(tmp_path: Path, monkeypatch) -> None:
    registry = core.Registry()
    registry.counter("demo_write_total").inc()
    monkeypatch.setattr(core, "_REGISTRY", registry)

    target = tmp_path / "metrics.prom"
    core.write_metrics(target)

    payload = target.read_text(encoding="utf-8")
    assert payload.strip() != ""
