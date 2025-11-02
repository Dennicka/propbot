"""Tests for the core account health primitives."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry

from app.config.schema import HealthConfig
from app.health.account_health import (
    AccountHealthSnapshot,
    evaluate_health,
    register_metrics,
    update_metrics,
)


def _snapshot(
    *,
    exchange: str = "binance",
    equity_usdt: float = 1000.0,
    free_collateral_usdt: float = 250.0,
    init_margin_usdt: float = 100.0,
    maint_margin_usdt: float = 50.0,
    margin_ratio: float = 0.5,
    ts: float = 1_700_000_000.0,
) -> AccountHealthSnapshot:
    return AccountHealthSnapshot(
        exchange=exchange,
        equity_usdt=equity_usdt,
        free_collateral_usdt=free_collateral_usdt,
        init_margin_usdt=init_margin_usdt,
        maint_margin_usdt=maint_margin_usdt,
        margin_ratio=margin_ratio,
        ts=ts,
    )


def test_evaluate_health_thresholds() -> None:
    cfg = SimpleNamespace(health=HealthConfig())

    assert evaluate_health(_snapshot(), cfg) == "OK"
    assert (
        evaluate_health(_snapshot(margin_ratio=0.80), cfg)
        == "WARN"
    )
    assert (
        evaluate_health(_snapshot(margin_ratio=0.90), cfg)
        == "CRITICAL"
    )
    assert (
        evaluate_health(_snapshot(free_collateral_usdt=80.0), cfg)
        == "WARN"
    )
    assert (
        evaluate_health(_snapshot(free_collateral_usdt=5.0), cfg)
        == "CRITICAL"
    )


def test_metrics_update_shapes() -> None:
    registry = CollectorRegistry()
    register_metrics(registry)

    snapshots = {
        "binance": _snapshot(margin_ratio=0.5, free_collateral_usdt=250.0),
        "okx": _snapshot(
            exchange="okx",
            margin_ratio=float("nan"),
            maint_margin_usdt=75.0,
            equity_usdt=500.0,
            free_collateral_usdt=40.0,
        ),
    }
    states = {
        "binance": "OK",
        "okx": "WARN",
        "bybit": "CRITICAL",
    }

    update_metrics(snapshots, states)

    assert registry.get_sample_value(
        "propbot_account_health_margin_ratio", {"exchange": "binance"}
    ) == pytest.approx(0.5)
    # okx ratio falls back to maint/equity -> 0.15
    assert registry.get_sample_value(
        "propbot_account_health_margin_ratio", {"exchange": "okx"}
    ) == pytest.approx(0.15)

    assert registry.get_sample_value(
        "propbot_account_health_free_collateral_usd", {"exchange": "okx"}
    ) == pytest.approx(40.0)

    assert registry.get_sample_value(
        "propbot_account_health_state", {"exchange": "binance", "state": "OK"}
    ) == 1.0
    assert registry.get_sample_value(
        "propbot_account_health_state", {"exchange": "okx", "state": "WARN"}
    ) == 1.0
    assert registry.get_sample_value(
        "propbot_account_health_state", {"exchange": "bybit", "state": "CRITICAL"}
    ) == 1.0
