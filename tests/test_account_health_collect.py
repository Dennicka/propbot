"""Tests for collecting account health snapshots from adapters."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry

from app.config.schema import HealthConfig
from app.health.account_health import (
    AccountHealthSnapshot,
    collect_account_health,
    register_metrics,
)


class _SnapshotAdapter:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def account_snapshot(self) -> dict[str, object]:
        return dict(self._payload)


class _StateAdapter:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def get_account_state(self) -> dict[str, object]:
        return dict(self._payload)


class _AttributeAdapter:
    def __init__(self, payload: dict[str, object]) -> None:
        self.account_state = dict(payload)


@pytest.fixture
def fake_ctx() -> SimpleNamespace:
    adapters = {
        "binance": _SnapshotAdapter(
            {
                "equity": 2000.0,
                "freeCollateral": 1750.0,
                "initialMargin": 250.0,
                "maintenanceMargin": 150.0,
                "marginRatio": 0.075,
            }
        ),
        "okx": _StateAdapter(
            {
                "totalEquity": 800.0,
                "availableBalance": 500.0,
                "totalInitialMargin": 200.0,
                "totalMaintMargin": 100.0,
            }
        ),
        "bybit": _AttributeAdapter(
            {
                "raw": {
                    "netValue": 1500.0,
                    "cashBal": 900.0,
                    "totalInitialMargin": 300.0,
                    "totalMaintMargin": 75.0,
                    "marginRatio": 0.05,
                }
            }
        ),
    }

    class _Ctx(SimpleNamespace):
        def brokers(self) -> dict[str, object]:
            return adapters

    return _Ctx(health=HealthConfig())


def test_snapshot_unifies_from_adapters(fake_ctx: SimpleNamespace) -> None:
    snapshots = collect_account_health(fake_ctx)

    assert set(snapshots) == {"binance", "okx", "bybit"}
    assert all(isinstance(snapshot, AccountHealthSnapshot) for snapshot in snapshots.values())

    binance = snapshots["binance"]
    assert binance.equity_usdt == pytest.approx(2000.0)
    assert binance.free_collateral_usdt == pytest.approx(1750.0)
    assert binance.init_margin_usdt == pytest.approx(250.0)
    assert binance.maint_margin_usdt == pytest.approx(150.0)
    assert binance.margin_ratio == pytest.approx(0.075)

    okx = snapshots["okx"]
    assert okx.equity_usdt == pytest.approx(800.0)
    assert okx.free_collateral_usdt == pytest.approx(500.0)
    assert okx.init_margin_usdt == pytest.approx(200.0)
    assert okx.maint_margin_usdt == pytest.approx(100.0)
    assert okx.margin_ratio == pytest.approx(0.125)

    bybit = snapshots["bybit"]
    assert bybit.equity_usdt == pytest.approx(1500.0)
    assert bybit.free_collateral_usdt == pytest.approx(900.0)
    assert bybit.init_margin_usdt == pytest.approx(300.0)
    assert bybit.maint_margin_usdt == pytest.approx(75.0)
    assert bybit.margin_ratio == pytest.approx(0.05)


def test_metrics_after_collect(fake_ctx: SimpleNamespace) -> None:
    registry = CollectorRegistry()
    register_metrics(registry)

    collect_account_health(fake_ctx)

    assert registry.get_sample_value(
        "propbot_account_health_margin_ratio", {"exchange": "binance"}
    ) == pytest.approx(0.075)
    assert registry.get_sample_value(
        "propbot_account_health_margin_ratio", {"exchange": "okx"}
    ) == pytest.approx(0.125)
    assert registry.get_sample_value(
        "propbot_account_health_free_collateral_usd", {"exchange": "bybit"}
    ) == pytest.approx(900.0)
    assert registry.get_sample_value(
        "propbot_account_health_state", {"exchange": "binance", "state": "OK"}
    ) == 1.0
