from __future__ import annotations

import json

import pytest

from app.strategy_risk import (
    DEFAULT_LIMITS,
    StrategyRiskManager,
    get_strategy_risk_manager,
    reset_strategy_risk_manager_for_tests,
)
from app.services.arbitrage import execute_trade


def test_strategy_risk_manager_breach_detection() -> None:
    manager = StrategyRiskManager()

    baseline = manager.check_limits("cross_exchange_arb")
    assert baseline["breach"] is False

    manager.record_fill("cross_exchange_arb", -200.0)
    still_ok = manager.check_limits("cross_exchange_arb")
    assert still_ok["breach"] is False

    manager.record_fill("cross_exchange_arb", -400.0)
    loss_breach = manager.check_limits("cross_exchange_arb")
    assert loss_breach["breach"] is True
    assert any("realized_pnl_today" in reason for reason in loss_breach["breach_reasons"])

    tuned_limits = {
        "cross_exchange_arb": {
            "daily_loss_usdt": DEFAULT_LIMITS["cross_exchange_arb"]["daily_loss_usdt"],
            "max_consecutive_failures": 2,
        }
    }
    failure_manager = StrategyRiskManager(limits=tuned_limits)
    failure_manager.record_failure("cross_exchange_arb", "test failure 1")
    no_failure_breach = failure_manager.check_limits("cross_exchange_arb")
    assert no_failure_breach["breach"] is False

    failure_manager.record_failure("cross_exchange_arb", "test failure 2")
    still_under_limit = failure_manager.check_limits("cross_exchange_arb")
    assert still_under_limit["breach"] is False

    failure_manager.record_failure("cross_exchange_arb", "test failure 3")
    failure_breach = failure_manager.check_limits("cross_exchange_arb")
    assert failure_breach["breach"] is True
    assert any("consecutive_failures" in reason for reason in failure_breach["breach_reasons"])


@pytest.mark.usefixtures("client")
def test_risk_status_endpoint_requires_token(client, monkeypatch) -> None:
    reset_strategy_risk_manager_for_tests()
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "unit-test-token")

    unauthorized = client.get("/api/ui/risk_status")
    assert unauthorized.status_code == 401

    authorized = client.get(
        "/api/ui/risk_status",
        headers={"Authorization": "Bearer unit-test-token"},
    )
    assert authorized.status_code == 200
    payload = authorized.json()
    assert "strategies" in payload
    assert "cross_exchange_arb" in payload["strategies"]
    assert isinstance(payload.get("timestamp"), str)


def test_strategy_auto_freeze_blocks_execution(monkeypatch) -> None:
    reset_strategy_risk_manager_for_tests()
    calls: list[dict[str, object]] = []

    def _log(operator_name: str, role: str, action: str, details=None) -> None:
        calls.append(
            {
                "operator_name": operator_name,
                "role": role,
                "action": action,
                "details": details,
            }
        )

    monkeypatch.setattr("app.strategy_risk.audit_log.log_operator_action", _log)

    manager = get_strategy_risk_manager()
    manager.limits.setdefault("cross_exchange_arb", {})["max_consecutive_failures"] = 0

    manager.record_failure("cross_exchange_arb", "synthetic failure")

    assert manager.is_frozen("cross_exchange_arb") is True
    assert any(call["action"] == "STRATEGY_AUTO_FREEZE" for call in calls)

    execution = execute_trade(pair_id=None, size=None)
    assert execution["ok"] is False
    assert execution["state"] == "BLOCKED"
    assert execution["reason"] == "blocked_by_risk_freeze"
    assert execution.get("executed") is False

    manager.record_success("cross_exchange_arb")
    snapshot = manager.check_limits("cross_exchange_arb")
    assert snapshot["snapshot"]["consecutive_failures"] == 0
    assert manager.is_frozen("cross_exchange_arb") is True


def test_unfreeze_endpoint_enforces_operator_role(monkeypatch, tmp_path, client) -> None:
    reset_strategy_risk_manager_for_tests()
    calls: list[dict[str, object]] = []

    def _log(operator_name: str, role: str, action: str, details=None) -> None:
        calls.append(
            {
                "operator_name": operator_name,
                "role": role,
                "action": action,
                "details": details,
            }
        )

    monkeypatch.setattr("app.strategy_risk.audit_log.log_operator_action", _log)

    manager = get_strategy_risk_manager()
    manager.limits["test_strategy"] = {"max_consecutive_failures": 0}
    manager.record_failure("test_strategy", "synthetic failure")
    assert manager.is_frozen("test_strategy") is True

    secrets_payload = {
        "operator_tokens": {
            "viewer": {"token": "VIEW", "role": "viewer"},
            "operator": {"token": "OPER", "role": "operator"},
        }
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")

    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)

    viewer_response = client.post(
        "/api/ui/unfreeze-strategy",
        json={"strategy": "test_strategy", "reason": "viewer attempt"},
        headers={"Authorization": "Bearer VIEW"},
    )
    assert viewer_response.status_code == 403
    assert manager.is_frozen("test_strategy") is True

    operator_response = client.post(
        "/api/ui/unfreeze-strategy",
        json={"strategy": "test_strategy", "reason": "operator override"},
        headers={"Authorization": "Bearer OPER"},
    )
    assert operator_response.status_code == 200
    payload = operator_response.json()
    assert payload["frozen"] is False
    assert manager.is_frozen("test_strategy") is False
    assert any(call["action"] == "STRATEGY_UNFREEZE_MANUAL" for call in calls)
