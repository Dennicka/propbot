from fastapi.testclient import TestClient

from app.server_ws import app


client = TestClient(app)


def test_ui_pnl_has_fee_and_funding_fields() -> None:
    response = client.get("/api/ui/pnl")
    assert response.status_code == 200

    payload = response.json()
    for key in ("realized", "unrealized", "fees_paid", "funding_paid", "gross_pnl", "net_pnl"):
        assert key in payload

    positions = payload.get("positions", [])
    if not positions:
        return

    row = positions[0]
    for key in ("fees_paid", "funding_paid", "gross_pnl", "net_pnl"):
        assert key in row
