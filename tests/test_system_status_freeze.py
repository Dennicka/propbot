import time

from app.risk.freeze import FreezeRule, get_freeze_registry, reset_freeze_registry


def test_system_status_includes_freeze(client):
    reset_freeze_registry()
    registry = get_freeze_registry()
    registry.apply(FreezeRule(reason="HEALTH_CRITICAL::BINANCE", scope="venue", ts=time.time()))

    response = client.get("/api/ui/system_status")
    assert response.status_code == 200
    payload = response.json()
    assert "freeze" in payload
    freeze_info = payload["freeze"]
    assert freeze_info["active"] is True
    assert any(rule["reason"].startswith("HEALTH_CRITICAL::") for rule in freeze_info["rules"])


def test_system_status_freeze_empty_when_cleared(client):
    reset_freeze_registry()
    response = client.get("/api/ui/system_status")
    assert response.status_code == 200
    freeze_info = response.json().get("freeze")
    assert freeze_info
    assert freeze_info["active"] is False
    assert freeze_info["rules"] == []
