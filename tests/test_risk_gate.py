from __future__ import annotations

import pytest

from app.risk import core as risk_core
from app.risk.core import _RiskMetrics


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("RISK_CHECKS_ENABLED", raising=False)
    monkeypatch.delenv("MAX_TOTAL_NOTIONAL_USDT", raising=False)
    monkeypatch.delenv("MAX_TOTAL_NOTIONAL_USD", raising=False)
    monkeypatch.delenv("MAX_OPEN_POSITIONS", raising=False)
    risk_core.reset_risk_governor_for_tests()
    yield
    risk_core.reset_risk_governor_for_tests()


def _stub_metrics(total_notional: float, open_positions: int) -> _RiskMetrics:
    return _RiskMetrics(total_notional=total_notional, open_positions=open_positions)


def test_risk_gate_allows_when_flag_disabled(monkeypatch):
    monkeypatch.setattr(risk_core, "_current_risk_metrics", lambda: _stub_metrics(0.0, 0))

    result = risk_core.risk_gate({"intent_notional": 200.0})

    assert result["allowed"] is True
    assert result["reason"] == "disabled"


def test_risk_gate_allows_under_caps(monkeypatch):
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "1000")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")
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
    assert result["reason"] == "risk.max_notional"
    assert result["cap"] == "max_total_notional_usdt"
