from __future__ import annotations

import pytest

from types import SimpleNamespace

from app.risk import accounting as risk_accounting
from app.risk import core as risk_core
from app.risk import auto_hold
from app.risk.core import _RiskMetrics
from app.risk.telemetry import (
    get_risk_skip_counts,
    reset_risk_skip_metrics_for_tests,
)
from app.services import runtime


def _clear_runtime_hold() -> None:
    if runtime.is_hold_active():
        request = runtime.record_resume_request("release_for_test", requested_by="pytest")
        runtime.approve_resume(request_id=request["id"], actor="pytest")


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


def test_daily_loss_auto_hold_engages_hold(monkeypatch):
    runtime.reset_for_tests()
    _clear_runtime_hold()
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()
    monkeypatch.setattr(risk_core, "get_state", runtime.get_state)
    monkeypatch.setattr(
        risk_core,
        "_current_risk_metrics",
        lambda: _RiskMetrics(total_notional=0.0, open_positions=0),
    )

    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("ENFORCE_DAILY_LOSS_CAP", "1")
    monkeypatch.setenv("DAILY_LOSS_CAP_AUTO_HOLD", "1")
    monkeypatch.setenv("DAILY_LOSS_CAP_USDT", "100")

    state = runtime.get_state()
    state.control.dry_run = False
    state.control.dry_run_mode = False

    audit_events: list[dict[str, object]] = []
    alerts: list[dict[str, object]] = []

    def _log(operator: str, role: str, action: str, details=None) -> None:
        audit_events.append(
            {
                "operator": operator,
                "role": role,
                "action": action,
                "details": dict(details or {}),
            }
        )

    def _alert(kind: str, text: str, extra=None) -> None:
        alerts.append({"kind": kind, "text": text, "extra": dict(extra or {})})

    monkeypatch.setattr(auto_hold, "log_operator_action", _log)
    monkeypatch.setattr(auto_hold, "send_notifier_alert", _alert)

    risk_accounting.record_fill("unit_test", 0.0, -150.0, simulated=False)

    intent = {"intent_notional": 1.0, "intent_open_positions": 0, "strategy": "unit_test"}
    result = risk_core.risk_gate(intent)

    assert result["allowed"] is False
    assert result.get("hold_engaged") is True
    assert result.get("status") == "hold_engaged"

    state_after = runtime.get_state()
    assert state_after.safety.hold_active is True
    assert state_after.safety.hold_reason == auto_hold.AUTO_HOLD_REASON

    assert any(event["action"] == auto_hold.AUTO_HOLD_ACTION for event in audit_events)
    recorded = next(
        event for event in audit_events if event["action"] == auto_hold.AUTO_HOLD_ACTION
    )
    assert recorded["details"].get("reason") == auto_hold.AUTO_HOLD_AUDIT_REASON
    assert alerts and alerts[0]["kind"] == auto_hold.AUTO_HOLD_ALERT_KIND


def test_daily_loss_auto_hold_skipped_when_flag_disabled(monkeypatch):
    runtime.reset_for_tests()
    _clear_runtime_hold()
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()
    monkeypatch.setattr(risk_core, "get_state", runtime.get_state)
    monkeypatch.setattr(
        risk_core,
        "_current_risk_metrics",
        lambda: _RiskMetrics(total_notional=0.0, open_positions=0),
    )

    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("ENFORCE_DAILY_LOSS_CAP", "1")
    monkeypatch.setenv("DAILY_LOSS_CAP_USDT", "100")

    audit_events: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        auto_hold,
        "log_operator_action",
        lambda name, role, action, details=None: audit_events.append((name, role, action)),
    )

    risk_accounting.record_fill("unit_test", 0.0, -150.0, simulated=False)

    intent = {"intent_notional": 1.0, "intent_open_positions": 0, "strategy": "unit_test"}
    result = risk_core.risk_gate(intent)

    assert not result.get("hold_engaged")
    assert runtime.get_state().safety.hold_active is False
    assert audit_events == []


def test_daily_loss_auto_hold_skipped_in_dry_run(monkeypatch):
    runtime.reset_for_tests()
    _clear_runtime_hold()
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()
    monkeypatch.setattr(risk_core, "get_state", runtime.get_state)
    monkeypatch.setattr(
        risk_core,
        "_current_risk_metrics",
        lambda: _RiskMetrics(total_notional=0.0, open_positions=0),
    )

    monkeypatch.setenv("RISK_CHECKS_ENABLED", "1")
    monkeypatch.setenv("ENFORCE_DAILY_LOSS_CAP", "1")
    monkeypatch.setenv("DAILY_LOSS_CAP_AUTO_HOLD", "1")
    monkeypatch.setenv("DAILY_LOSS_CAP_USDT", "100")

    state = runtime.get_state()
    state.control.dry_run = True
    state.control.dry_run_mode = False

    alerts: list[str] = []
    monkeypatch.setattr(
        auto_hold, "send_notifier_alert", lambda *args, **kwargs: alerts.append(args[0])
    )

    risk_accounting.record_fill("unit_test", 0.0, -150.0, simulated=False)

    intent = {"intent_notional": 1.0, "intent_open_positions": 0, "strategy": "unit_test"}
    result = risk_core.risk_gate(intent)

    assert not result.get("hold_engaged")
    assert runtime.get_state().safety.hold_active is False
    assert alerts == []
