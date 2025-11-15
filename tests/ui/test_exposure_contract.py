from __future__ import annotations

from fastapi.testclient import TestClient

from app.server_ws import app


client = TestClient(app)


def test_ui_exposure_contract_smoke() -> None:
    response = client.get("/api/ui/exposure")
    assert response.status_code == 200

    data = response.json()

    assert isinstance(data, dict)
    assert "per_venue" in data
    assert "total" in data

    per_venue = data["per_venue"]
    assert isinstance(per_venue, dict)

    total = data["total"]
    assert isinstance(total, (int, float))

    if per_venue:
        venue, payload = next(iter(per_venue.items()))
        assert isinstance(venue, str)
        assert isinstance(payload, dict)
        assert "net_usdt" in payload or "notional" in payload or "net_exposure" in payload
