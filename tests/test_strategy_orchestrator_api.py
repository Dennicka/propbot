from __future__ import annotations

import json
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from app.strategy_orchestrator import StrategyOrchestrator, reset_strategy_orchestrator


@pytest.fixture
def authed_client(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> TestClient:
    secrets_path = tmp_path / "secrets.json"
    secrets_payload = {
        "operator_tokens": {
            "alice": {"token": "operator-token", "role": "operator"},
            "bob": {"token": "viewer-token", "role": "viewer"},
        }
    }
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")
    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    reset_strategy_orchestrator(StrategyOrchestrator())
    return client


def test_strategy_toggle_rbac_and_status(authed_client: TestClient) -> None:
    viewer_headers = {"Authorization": "Bearer viewer-token"}
    operator_headers = {"Authorization": "Bearer operator-token"}
    enable_payload = {"strategy": "cross_exchange_arb", "reason": "scheduled"}

    forbidden = authed_client.post(
        "/api/ui/strategy/enable", json=enable_payload, headers=viewer_headers
    )
    assert forbidden.status_code == 403

    enabled = authed_client.post(
        "/api/ui/strategy/enable", json=enable_payload, headers=operator_headers
    )
    assert enabled.status_code == 200
    assert "cross_exchange_arb" in enabled.json().get("enabled_strategies", [])

    status = authed_client.get("/api/ui/strategy/status", headers=operator_headers)
    assert status.status_code == 200
    payload = status.json()
    assert payload["orchestrator"]["enabled_strategies"] == ["cross_exchange_arb"]
