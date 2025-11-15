from __future__ import annotations

from fastapi.testclient import TestClient

from app.server_ws import app


client = TestClient(app)


def test_health_endpoint_smoke() -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    expected = {"ok", "journal_ok", "resume_ok", "leader", "config_ok"}
    assert expected.issubset(data)
    if "watchdog" in data:
        assert isinstance(data["watchdog"], dict)


def test_ui_status_smoke() -> None:
    resp = client.get("/api/ui/status/full")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    assert "config" in data
    cfg = data["config"]
    assert isinstance(cfg, dict)
    for key in ("runtime", "router", "risk_limits"):
        assert key in cfg


def test_ui_config_smoke() -> None:
    resp = client.get("/api/ui/config")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    assert "config" in data
    cfg = data["config"]
    assert isinstance(cfg, dict)
    for key in ("runtime", "router", "risk_limits"):
        assert key in cfg


def test_ui_alerts_smoke() -> None:
    resp = client.get("/api/ui/alerts")
    assert resp.status_code == 200
    items = resp.json()
    assert isinstance(items, list)
    if items:
        alert = items[0]
        assert isinstance(alert, dict)
        for key in ("ts", "event_type", "message", "severity"):
            assert key in alert


def test_ui_execution_smoke() -> None:
    resp = client.get("/api/ui/execution")
    assert resp.status_code == 200
    data = resp.json()
    if isinstance(data, dict) and "orders" in data:
        items = data["orders"]
    else:
        items = data
    assert isinstance(items, list)
