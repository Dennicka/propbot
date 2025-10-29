from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.exchange_watchdog import ExchangeWatchdog
from app.routers import exchange_watchdog as exchange_watchdog_router


def test_exchange_health_endpoint_allows_viewer(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "viewer-token")

    watchdog = ExchangeWatchdog()
    watchdog.update_from_client("binance", ok=False, rate_limited=True, error="rate_limit")

    def _mock_get_watchdog():
        return watchdog

    def _mock_resolve_operator_identity(token: str):
        if token == "viewer-token":
            return ("pytest-viewer", "viewer")
        return None

    monkeypatch.setattr(exchange_watchdog_router, "get_exchange_watchdog", _mock_get_watchdog)
    monkeypatch.setattr(
        exchange_watchdog_router,
        "resolve_operator_identity",
        _mock_resolve_operator_identity,
    )

    app = FastAPI()
    app.include_router(exchange_watchdog_router.router, prefix="/api/ui")

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
    assert payload["binance"].get("error") == "rate_limit"
