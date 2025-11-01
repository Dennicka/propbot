from __future__ import annotations

import pytest


async def _sample_payload() -> dict[str, object]:
    return {
        "generated_at": "2024-01-01T00:00:00+00:00",
        "by_strategy": {"alpha": {"realized": 10.0, "unrealized": 1.0, "fees": 0.2, "rebates": 0.05, "funding": 0.1, "net": 10.95}},
        "by_venue": {"binance": {"realized": 9.0, "unrealized": 0.5, "fees": 0.1, "rebates": 0.02, "funding": 0.0, "net": 9.42}},
        "totals": {"realized": 10.0, "unrealized": 1.0, "fees": 0.2, "rebates": 0.05, "funding": 0.1, "net": 10.95},
        "meta": {"exclude_simulated": True},
    }


def test_pnl_attrib_requires_token(monkeypatch: pytest.MonkeyPatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret")
    response = client.get("/api/ui/pnl_attrib")
    assert response.status_code == 401


def test_pnl_attrib_returns_payload(monkeypatch: pytest.MonkeyPatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret")
    monkeypatch.setattr("app.routers.ui_pnl_attrib.build_pnl_attribution", _sample_payload)

    response = client.get("/api/ui/pnl_attrib", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"]["realized"] == pytest.approx(10.0)
    assert payload["by_strategy"]["alpha"]["fees"] == pytest.approx(0.2)
