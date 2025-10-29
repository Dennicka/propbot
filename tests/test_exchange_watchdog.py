from __future__ import annotations

from fastapi.testclient import TestClient

from app.exchange_watchdog import ExchangeWatchdog
from app.main import app


def test_exchange_health_endpoint_viewer_access(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "viewer-token")

    watchdog = ExchangeWatchdog()
    watchdog.update_from_client("binance", ok=False, rate_limited=True, error="429")

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
        "/api/ui/exchange_health",
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "binance" in payload
    assert payload["binance"]["reachable"] is False
    assert payload["binance"]["rate_limited"] is True
    assert payload["binance"]["error"] == "429"
