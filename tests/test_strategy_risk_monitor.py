from __future__ import annotations

import pytest

from app.strategy_risk import (
    DEFAULT_LIMITS,
    StrategyRiskManager,
    reset_strategy_risk_manager_for_tests,
)


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
