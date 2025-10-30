from types import SimpleNamespace

import json

import pytest

from app.risk import accounting as risk_accounting
from app.risk import core as risk_core


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.setattr(
        risk_accounting,
        "get_state",
        lambda: SimpleNamespace(control=SimpleNamespace(dry_run=False, dry_run_mode=False)),
    )
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()
    yield
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()


def test_budget_reset_endpoint(monkeypatch, tmp_path, client):
    secrets_payload = {
        "operator_tokens": {
            "alice": {"token": "AAA", "role": "operator"},
        }
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")

    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)

    risk_accounting.set_strategy_budget_cap("alpha", 20.0)
    risk_accounting.record_fill("alpha", 0.0, -15.0, simulated=False)

    logged_actions: list[tuple[str, str, str]] = []

    def fake_log_operator_action(name: str, role: str, action: str, details=None):
        logged_actions.append((name, role, action))

    monkeypatch.setattr("app.routers.ui.log_operator_action", fake_log_operator_action)

    response = client.post(
        "/api/ui/budget/reset",
        headers={"Authorization": "Bearer AAA"},
        json={"strategy": "alpha", "reason": "ops-reset"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["strategy"] == "alpha"
    budget = payload["budget"]
    assert budget["used_today_usdt"] == pytest.approx(0.0)
    assert budget["blocked_by_budget"] is False
    assert any(action == "BUDGET_RESET" for _, _, action in logged_actions)
