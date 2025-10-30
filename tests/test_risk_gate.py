from __future__ import annotations

import pytest

from types import SimpleNamespace

from app.risk import accounting as risk_accounting
from app.risk import core as risk_core
from app.risk.core import _RiskMetrics
from app.risk.telemetry import (
    get_risk_skip_counts,
    reset_risk_skip_metrics_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("RISK_CHECKS_ENABLED", raising=False)
    monkeypatch.delenv("MAX_TOTAL_NOTIONAL_USDT", raising=False)
    monkeypatch.delenv("MAX_TOTAL_NOTIONAL_USD", raising=False)
    monkeypatch.delenv("MAX_OPEN_POSITIONS", raising=False)
    monkeypatch.delenv("RISK_ENFORCE_CAPS", raising=False)
    monkeypatch.delenv("RISK_ENFORCE_BUDGETS", raising=False)
    monkeypatch.delenv("DRY_RUN_MODE", raising=False)
    monkeypatch.delenv("DAILY_LOSS_CAP_USDT", raising=False)
    monkeypatch.delenv("ENFORCE_DAILY_LOSS_CAP", raising=False)
    risk_core.reset_risk_governor_for_tests()
    risk_accounting.reset_risk_accounting_for_tests()
    reset_risk_skip_metrics_for_tests()
    monkeypatch.setattr(
        risk_core,
        "get_state",
        lambda: SimpleNamespace(control=SimpleNamespace(dry_run=False)),
    )
    yield
    risk_core.reset_risk_governor_for_tests()
    risk_accounting.reset_risk_accounting_for_tests()
    reset_risk_skip_metrics_for_tests()


def _stub_metrics(total_notional: float, open_positions: int) -> _RiskMetrics:
    return _RiskMetrics(total_notional=total_notional, open_positions=open_positions)


def test_risk_gate_allows_when_flag_disabled(monkeypatch):
    monkeypatch.setattr(risk_core, "_current_risk_metrics", lambda: _stub_metrics(0.0, 0))
    monkeypatch.setenv("RISK_ENFORCE_CAPS", "1")

    result = risk_core.risk_gate({"intent_notional": 200.0})

    assert result["allowed"] is True
    assert result["reason"] == "risk_checks_disabled"


def test_risk_gate_allows_under_caps(monkeypatch):
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "1000")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")
    monkeypatch.setenv("RISK_ENFORCE_CAPS", "1")
    risk_core.reset_risk_governor_for_tests()
    monkeypatch.setattr(risk_core, "_current_risk_metrics", lambda: _stub_metrics(100.0, 1))

    intent = {
        "intent_notional": 200.0,
        "intent_open_positions": 1,
        "symbol": "BTCUSDT",
        "venue": "manual_test",
        "strategy": "unit_test",
        "side": "buy",
    }
    result = risk_core.risk_gate(intent)

    assert result["allowed"] is True
    assert result["reason"] == "ok"


def test_risk_gate_blocks_when_caps_breached(monkeypatch):
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "1000")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "3")
    monkeypatch.setenv("RISK_ENFORCE_CAPS", "1")
    risk_core.reset_risk_governor_for_tests()
    monkeypatch.setattr(risk_core, "_current_risk_metrics", lambda: _stub_metrics(950.0, 3))

    intent = {
        "intent_notional": 200.0,
        "intent_open_positions": 1,
        "symbol": "BTCUSDT",
        "venue": "manual_test",
        "strategy": "unit_test",
        "side": "buy",
    }
    result = risk_core.risk_gate(intent)

    assert result["allowed"] is False
    assert result["reason"] == "caps_exceeded"
    assert result["state"] == "SKIPPED_BY_RISK"
    assert result["cap"] == "max_total_notional_usdt"
    assert result.get("details", {}).get("breach") == "max_total_notional_usdt"
    counts = get_risk_skip_counts()
    assert counts.get("unit_test", {}).get("caps_exceeded") == 1


def test_risk_gate_blocks_when_daily_loss_cap_breached(monkeypatch):
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("ENFORCE_DAILY_LOSS_CAP", "1")
    monkeypatch.setenv("DAILY_LOSS_CAP_USDT", "100")
    risk_core.reset_risk_governor_for_tests()
    monkeypatch.setattr(risk_core, "_current_risk_metrics", lambda: _stub_metrics(0.0, 0))

    risk_accounting.record_fill("unit_test", 0.0, -150.0, simulated=False)

    intent = {
        "intent_notional": 10.0,
        "intent_open_positions": 0,
        "strategy": "unit_test",
    }
    result = risk_core.risk_gate(intent)

    assert result["allowed"] is False
    assert result["reason"] == "DAILY_LOSS_CAP"
    assert result["state"] == "SKIPPED_BY_RISK"
    assert result.get("strategy") == "unit_test"
    details = result.get("details")
    assert isinstance(details, dict)
    assert "bot_loss_cap" in details
    assert "daily_loss_cap" in details
    counts = get_risk_skip_counts()
    assert counts.get("unit_test", {}).get("daily_loss_cap") == 1
