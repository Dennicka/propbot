from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.exchange_watchdog import ExchangeWatchdog
from app.routers import exchange_watchdog as exchange_watchdog_router


def test_exchange_health_endpoint_allows_viewer(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "viewer-token")

    watchdog = ExchangeWatchdog()

    def _probe() -> dict[str, object]:
        return {"binance": {"ok": False, "reason": "rate_limit"}}

    watchdog.check_once(_probe)

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
        "/api/ui/watchdog_status",
        headers={"Authorization": "Bearer viewer-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    exchanges = payload.get("exchanges", {})
    assert payload.get("overall_ok") is False
    assert exchanges["binance"]["ok"] is False
    assert exchanges["binance"].get("reason") == "rate_limit"
