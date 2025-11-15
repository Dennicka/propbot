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


def test_ui_exposure_flat_portfolio_has_zero_net_exposure() -> None:
    response = client.get("/api/ui/exposure")
    assert response.status_code == 200

    payload = response.json()
    per_venue = payload.get("per_venue", {})

    assert isinstance(per_venue, dict)
    assert payload.get("total") == 0.0

    for venue_payload in per_venue.values():
        assert isinstance(venue_payload, dict)
        net_value = venue_payload.get("net_usdt")
        assert net_value is not None
        assert abs(float(net_value)) < 1e-9
