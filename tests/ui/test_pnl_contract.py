from __future__ import annotations

from fastapi.testclient import TestClient

from app.server_ws import app


client = TestClient(app)


def test_ui_pnl_contract_smoke() -> None:
    response = client.get("/api/ui/pnl")
    assert response.status_code == 200

    data = response.json()

    assert isinstance(data, dict)
    for key in ("realized", "unrealized", "gross_pnl", "net_pnl", "positions"):
        assert key in data

    portfolio_net = data["net_pnl"]
    assert isinstance(portfolio_net, str)

    positions = data["positions"]
    assert isinstance(positions, list)

    if positions:
        position = positions[0]
        assert isinstance(position, dict)
        for key in ("symbol", "venue", "net_pnl"):
            assert key in position
        assert "gross_pnl" in position or "realized_pnl" in position
