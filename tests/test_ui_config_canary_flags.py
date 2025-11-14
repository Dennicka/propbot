from fastapi.testclient import TestClient

from app.services import runtime


def test_ui_config_exposes_canary_flags(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("EXEC_PROFILE", "paper")
    monkeypatch.setenv("CANARY_MODE", "1")
    monkeypatch.setenv("CANARY_PROFILE_NAME", "paper-canary")
    monkeypatch.setenv("FF_RISK_LIMITS", "1")
    runtime.reset_for_tests()

    response = client.get("/api/ui/config")
    assert response.status_code == 200

    payload = response.json()
    config = payload["config"]

    runtime_cfg = config["runtime"]
    assert runtime_cfg["is_canary"] is True
    assert runtime_cfg["display_name"] == "paper-canary"

    router_cfg = config["router"]
    assert router_cfg["canary_mode"] is True
    assert router_cfg["safe_mode_global"] is False
