from decimal import Decimal

from fastapi.testclient import TestClient

from app.pnl.models import PositionPnlSnapshot
from app.routers import ui_pnl
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


def test_ui_pnl_includes_fee_and_funding_fields(monkeypatch) -> None:
    positions = (
        PositionPnlSnapshot(
            symbol="BTCUSDT",
            venue="binance",
            realized_pnl=Decimal("10"),
            unrealized_pnl=Decimal("5"),
            fees_paid=Decimal("-1.5"),
            funding_paid=Decimal("0.25"),
        ),
        PositionPnlSnapshot(
            symbol="ETHUSDT",
            venue="binance",
            realized_pnl=Decimal("-3"),
            unrealized_pnl=Decimal("2"),
            fees_paid=Decimal("-0.5"),
            funding_paid=Decimal("-0.25"),
        ),
    )

    monkeypatch.setattr(ui_pnl, "_load_position_snapshots", lambda: positions)

    response = client.get("/api/ui/pnl")
    assert response.status_code == 200

    payload = response.json()
    expected_fees = sum((position.fees_paid for position in positions), Decimal("0"))
    expected_funding = sum((position.funding_paid for position in positions), Decimal("0"))
    expected_gross = sum((position.gross_pnl for position in positions), Decimal("0"))
    expected_net = expected_gross + expected_fees + expected_funding

    assert payload["fees_paid"] == str(expected_fees)
    assert payload["funding_paid"] == str(expected_funding)
    assert payload["gross_pnl"] == str(expected_gross)
    assert payload["net_pnl"] == str(expected_net)

    assert Decimal(payload["net_pnl"]) == (
        Decimal(payload["realized"])
        + Decimal(payload["unrealized"])
        + Decimal(payload["fees_paid"])
        + Decimal(payload["funding_paid"])
    )

    position_payload = payload["positions"][0]
    expected_position_net = positions[0].net_pnl
    assert position_payload["net_pnl"] == str(expected_position_net)
    assert Decimal(position_payload["net_pnl"]) == (
        Decimal(position_payload["realized_pnl"])
        + Decimal(position_payload["unrealized_pnl"])
        + Decimal(position_payload["fees_paid"])
        + Decimal(position_payload["funding_paid"])
    )


def test_ui_pnl_zero_portfolio_returns_zeroes(monkeypatch) -> None:
    monkeypatch.setattr(ui_pnl, "_load_position_snapshots", lambda: ())

    response = client.get("/api/ui/pnl")
    assert response.status_code == 200

    payload = response.json()

    assert payload["realized"] == "0"
    assert payload["unrealized"] == "0"
    assert payload["fees_paid"] == "0"
    assert payload["funding_paid"] == "0"
    assert payload["gross_pnl"] == "0"
    assert payload["net_pnl"] == "0"
    assert payload["positions"] == []
