from __future__ import annotations

from app import ledger


def test_alerts_endpoint_requires_auth(client, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    ledger.record_event(
        level="INFO",
        code="unit_test",
        payload={"message": "auth protected"},
    )

    unauth = client.get("/api/ui/alerts")
    assert unauth.status_code == 401

    authed = client.get(
        "/api/ui/alerts",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert authed.status_code == 200
    payload = authed.json()
    assert payload["total"] >= 1
    assert payload["items"]
    first = payload["items"][0]
    assert first["code"] == "unit_test"
    assert "message" in first
