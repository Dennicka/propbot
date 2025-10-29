from __future__ import annotations

import json

from app.strategy_budget import (
    StrategyBudgetManager,
    reset_strategy_budget_manager_for_tests,
)


def test_strategy_budget_allocation_blocks_when_limit_reached() -> None:
    manager = StrategyBudgetManager(
        initial_budgets={
            "demo": {
                "max_notional_usdt": 1_000.0,
                "max_open_positions": 2,
                "current_notional_usdt": 0.0,
                "current_open_positions": 0,
            }
        }
    )
    reset_strategy_budget_manager_for_tests(manager)
    try:
        assert manager.can_allocate("demo", 900.0)
        manager.reserve("demo", 900.0)
        assert manager.can_allocate("demo", 500.0) is False
    finally:
        reset_strategy_budget_manager_for_tests()


def test_strategy_budget_endpoint_allows_viewer(client, monkeypatch, tmp_path) -> None:
    secrets_payload = {
        "operator_tokens": {
            "alice": {"token": "AAA", "role": "operator"},
            "bob": {"token": "BBB", "role": "viewer"},
        }
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")

    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(tmp_path / "runtime.json"))

    manager = StrategyBudgetManager(
        initial_budgets={
            "demo": {
                "max_notional_usdt": 1_000.0,
                "max_open_positions": 2,
                "current_notional_usdt": 0.0,
                "current_open_positions": 0,
            }
        }
    )
    reset_strategy_budget_manager_for_tests(manager)
    try:
        manager.reserve("demo", 600.0)
        headers = {"Authorization": "Bearer BBB"}
        response = client.get("/api/ui/strategy_budget", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert "strategies" in payload
        assert "snapshot" in payload
        entry = next(item for item in payload["strategies"] if item["strategy"] == "demo")
        assert entry["current_notional_usdt"] == 600.0
        assert entry["blocked"] is False
        assert payload["snapshot"]["demo"]["current_notional_usdt"] == 600.0
    finally:
        reset_strategy_budget_manager_for_tests()
