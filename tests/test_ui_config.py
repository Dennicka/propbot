from __future__ import annotations

from fastapi.testclient import TestClient

from app.services import runtime


def test_get_ui_config_basic(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("EXEC_PROFILE", "paper")
    monkeypatch.setenv("FF_RISK_LIMITS", "1")
    runtime.reset_for_tests()

    response = client.get("/api/ui/config")
    assert response.status_code == 200

    payload = response.json()
    assert "config" in payload

    config = payload["config"]
    assert "runtime" in config
    assert "router" in config
    assert "risk_limits" in config

    runtime_cfg = config["runtime"]
    assert isinstance(runtime_cfg.get("name"), str)
    assert runtime_cfg.get("name")

    router_cfg = config["router"]
    assert isinstance(router_cfg.get("mode"), str) or isinstance(router_cfg.get("safe_mode"), bool)

    risk_cfg = config["risk_limits"]
    assert "enabled" in risk_cfg
    assert isinstance(risk_cfg["enabled"], bool)
