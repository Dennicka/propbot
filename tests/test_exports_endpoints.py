from __future__ import annotations

import csv
from io import StringIO

from app import ledger
from app.services import portfolio
from app.services.portfolio import PortfolioBalance, PortfolioPosition, PortfolioSnapshot


def test_events_export_endpoints(client) -> None:
    ledger.reset()
    ledger.record_event(
        level="INFO",
        code="alpha",
        payload={"venue": "binance-um", "symbol": "BTCUSDT", "message": "Alpha"},
    )
    ledger.record_event(
        level="ERROR",
        code="beta",
        payload={"venue": "okx-perp", "symbol": "ETHUSDT", "message": "Beta"},
    )

    json_resp = client.get("/api/ui/events/export", params={"format": "json"})
    assert json_resp.status_code == 200
    items = json_resp.json()
    assert isinstance(items, list)
    assert items
    assert {"ts", "level", "type", "message"} <= set(items[0].keys())

    csv_resp = client.get("/api/ui/events/export", params={"format": "csv"})
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "events.csv" in csv_resp.headers["content-disposition"]
    reader = csv.reader(StringIO(csv_resp.text))
    header = next(reader)
    assert header == ["ts", "venue", "type", "level", "symbol", "message"]

    bad_resp = client.get("/api/ui/events/export", params={"format": "yaml"})
    assert bad_resp.status_code == 422


def test_portfolio_export_formats(client, monkeypatch) -> None:
    snapshot = PortfolioSnapshot(
        positions=[
            PortfolioPosition(
                venue="binance-um",
                venue_type="paper",
                symbol="BTCUSDT",
                qty=1.5,
                notional=30000.0,
                entry_px=20000.0,
                mark_px=20500.0,
                upnl=750.0,
                rpnl=125.0,
            )
        ],
        balances=[
            PortfolioBalance(
                venue="binance-um",
                asset="USDT",
                free=1200.0,
                total=1500.0,
            )
        ],
        pnl_totals={"total": 875.0},
        notional_total=30000.0,
    )

    async def fake_snapshot(*_, **__):  # type: ignore[override]
        return snapshot

    monkeypatch.setattr(portfolio, "snapshot", fake_snapshot)

    json_resp = client.get("/api/ui/portfolio/export", params={"format": "json"})
    assert json_resp.status_code == 200
    payload = json_resp.json()
    assert payload["positions"][0]["qty"] == snapshot.positions[0].qty
    assert (
        payload["balances"][0]["locked"] == snapshot.balances[0].total - snapshot.balances[0].free
    )

    csv_resp = client.get("/api/ui/portfolio/export", params={"format": "csv"})
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "portfolio.csv" in csv_resp.headers["content-disposition"]
    text = csv_resp.text.strip().splitlines()
    assert text[0] == "[positions]"
    assert text[1] == "venue,symbol,qty,notional,entry,mark,upnl,rpnl"
    assert "[balances]" in text
