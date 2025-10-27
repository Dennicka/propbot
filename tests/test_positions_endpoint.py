from positions import create_position


def test_positions_endpoint_requires_token(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    response = client.get("/api/ui/positions")
    assert response.status_code == 401

    create_position(
        symbol="ETHUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=1000.0,
        entry_spread_bps=10.0,
        leverage=2.0,
        entry_long_price=1800.0,
        entry_short_price=1805.0,
    )

    response = client.get(
        "/api/ui/positions",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "positions" in payload
    assert "exposure" in payload
    assert "totals" in payload
    assert payload["positions"]
    entry = payload["positions"][0]
    assert entry["symbol"] == "ETHUSDT"
    assert entry["legs"]
    legs = entry["legs"]
    assert any(leg["side"] == "long" for leg in legs)
    assert any(leg["side"] == "short" for leg in legs)
    assert payload["exposure"].get("binance-um", {}).get("long_notional") > 0
    assert payload["exposure"].get("okx-perp", {}).get("short_notional") > 0
    assert isinstance(payload["totals"].get("unrealized_pnl_usdt"), (int, float))
