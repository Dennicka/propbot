from __future__ import annotations

from fastapi.testclient import TestClient

from app.exchange_watchdog import ExchangeWatchdog
from app.main import app


def test_exchange_health_endpoint_viewer_access(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "viewer-token")

    watchdog = ExchangeWatchdog()

    def _probe() -> dict[str, object]:
        return {"binance": {"ok": False, "reason": "offline"}}

    watchdog.check_once(_probe)

    def _mock_get_watchdog():
        return watchdog

    def _mock_resolve_operator_identity(token: str):
        if token == "viewer-token":
            return ("pytest-viewer", "viewer")
        return None

    monkeypatch.setattr("app.routers.exchange_watchdog.get_exchange_watchdog", _mock_get_watchdog)
    monkeypatch.setattr(
        "app.routers.exchange_watchdog.resolve_operator_identity",
        _mock_resolve_operator_identity,
    )

    client = TestClient(app)
    response = client.get(
        "/api/ui/watchdog_status",
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["overall_ok"] is False
    assert "binance" in payload["exchanges"]
    assert payload["exchanges"]["binance"]["ok"] is False
    assert payload["exchanges"]["binance"]["reason"] == "offline"
