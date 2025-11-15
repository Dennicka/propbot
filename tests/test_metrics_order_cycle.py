from __future__ import annotations

from decimal import Decimal

import pytest
from prometheus_client import CollectorRegistry, Histogram

from app.metrics import order_cycle
from app.orders.tracker import OrderState, OrderTracker


def test_observe_order_cycle_records_histogram_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = CollectorRegistry()
    histogram = Histogram(
        "order_cycle_seconds",
        "Order lifecycle duration from initial submit to terminal state",
        labelnames=("runtime_profile", "venue", "outcome"),
        buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
        registry=registry,
    )
    monkeypatch.setattr(order_cycle, "ORDER_CYCLE_SECONDS", histogram)

    order_cycle.observe_order_cycle(
        runtime_profile="paper",
        venue="binance",
        outcome="filled",
        seconds=0.25,
    )

    labels = {"runtime_profile": "paper", "venue": "binance", "outcome": "filled"}
    count = registry.get_sample_value("order_cycle_seconds_count", labels)
    total = registry.get_sample_value("order_cycle_seconds_sum", labels)

    assert count == pytest.approx(1.0)
    assert total == pytest.approx(0.25)


def test_order_terminal_update_observes_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = OrderTracker()

    observed: list[dict[str, object]] = []

    def _capture_observation(**kwargs: object) -> None:
        observed.append(dict(kwargs))

    monkeypatch.setattr("app.orders.tracker.observe_order_cycle", _capture_observation)
    monkeypatch.setattr("app.orders.tracker._get_runtime_profile_label", lambda: "testnet")

    tracker.process_order_event(
        "order-1",
        OrderState.NEW,
        venue="Binance",
        qty=Decimal("1"),
        now_ns=1_000_000_000,
    )
    tracker.process_order_event(
        "order-1",
        OrderState.FILLED,
        venue="Binance",
        now_ns=3_000_000_000,
    )

    assert observed
    sample = observed[0]
    assert sample["runtime_profile"] == "testnet"
    assert sample["venue"] == "binance"
    assert sample["outcome"] == "filled"
    assert sample["seconds"] == pytest.approx(2.0)
