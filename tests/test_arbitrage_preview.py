from app.exchanges import binance_um, okx_perp


def test_arbitrage_preview_structure(client, monkeypatch) -> None:
    monkeypatch.setattr(
        binance_um,
        "get_book",
        lambda symbol: {"bid": 20150.0, "ask": 20160.0, "ts": 1},
    )
    monkeypatch.setattr(
        okx_perp,
        "get_book",
        lambda symbol: {"bid": 20190.0, "ask": 20200.0, "ts": 1},
    )

    response = client.get(
        "/api/arb/preview",
        params={"symbol": "BTCUSDT", "notional": 50, "slippage_bps": 2},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "BTCUSDT"
    for key in [
        "symbol",
        "viable",
        "legs",
        "est_pnl_usdt",
        "est_pnl_bps",
        "used_fees_bps",
        "used_slippage_bps",
    ]:
        assert key in payload
