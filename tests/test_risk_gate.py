from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import orchestrator


def _make_state(total_notional: float, open_positions: int) -> SimpleNamespace:
    risk_snapshot = {
        "total_notional_usd": total_notional,
        "per_venue": {"binance": {"open_positions_count": open_positions}},
    }
    safety = SimpleNamespace(hold_active=False, risk_snapshot=risk_snapshot)
    control = SimpleNamespace(safe_mode=False, dry_run=False, dry_run_mode=False)
    autopilot = SimpleNamespace(enabled=False)
    return SimpleNamespace(safety=safety, control=control, autopilot=autopilot)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    monkeypatch.delenv("MAX_TOTAL_NOTIONAL_USDT", raising=False)
    monkeypatch.delenv("MAX_OPEN_POSITIONS", raising=False)


def test_risk_gate_blocks_total_notional(monkeypatch):
    state = _make_state(total_notional=900.0, open_positions=2)
    monkeypatch.setattr(orchestrator, "get_state", lambda: state)
    monkeypatch.setattr(orchestrator, "load_runtime_payload", lambda: {})
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "1000")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "10")

    result = orchestrator.risk_gate({"intent_notional": 200.0, "intent_open_positions": 1})

    assert result["ok"] is False
    assert result["reason"] == "risk.max_notional"


def test_risk_gate_blocks_open_positions(monkeypatch):
    state = _make_state(total_notional=100.0, open_positions=3)
    monkeypatch.setattr(orchestrator, "get_state", lambda: state)
    monkeypatch.setattr(orchestrator, "load_runtime_payload", lambda: {})
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "100000")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "3")

    result = orchestrator.risk_gate({"intent_notional": 50.0, "intent_open_positions": 2})

    assert result["ok"] is False
    assert result["reason"] == "risk.max_open_positions"


def test_risk_gate_allows_intent_within_caps(monkeypatch):
    state = _make_state(total_notional=400.0, open_positions=2)
    monkeypatch.setattr(orchestrator, "get_state", lambda: state)
    monkeypatch.setattr(orchestrator, "load_runtime_payload", lambda: {})
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "1200")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")

    result = orchestrator.risk_gate({"intent_notional": 200.0, "intent_open_positions": 1})

    assert result["ok"] is True
    assert result["reason"] is None
