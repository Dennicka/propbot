from fastapi.testclient import TestClient

from app.server_ws import app


client = TestClient(app)


def test_ui_exposure_includes_key_fields() -> None:
    response = client.get("/api/ui/exposure")
    assert response.status_code == 200

    payload = response.json()
    assert isinstance(payload, dict)
    assert "per_venue" in payload
    assert "total" in payload

    per_venue = payload["per_venue"]
    assert isinstance(per_venue, dict)

    if per_venue:
        venue, data = next(iter(per_venue.items()))
        assert isinstance(data, dict)
        for key in ("long_notional", "short_notional", "net_usdt"):
            assert key in data

    assert isinstance(payload["total"], (int, float))
