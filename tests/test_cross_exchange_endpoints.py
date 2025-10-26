from __future__ import annotations

from app.services.runtime import get_state
from positions import list_positions, reset_positions


def test_cross_preview_endpoint(client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.arb.check_spread",
        lambda symbol: {
            "symbol": symbol,
            "cheap": "binance",
            "expensive": "okx",
            "cheap_ask": 100.0,
            "expensive_bid": 102.5,
            "spread": 2.5,
        },
    )
    resp = client.post("/api/arb/preview", json={"symbol": "BTCUSDT", "min_spread": 1.5})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["spread"] == 2.5
    assert payload["meets_min_spread"] is True
    assert payload["long_exchange"] == "binance"
    assert payload["short_exchange"] == "okx"


def test_cross_execute_endpoint(client, monkeypatch):
    reset_positions()
    state = get_state()
    state.control.safe_mode = False
    monkeypatch.setattr(
        "app.routers.arb.can_open_new_position", lambda notion, leverage: (True, "")
    )
    trade_result = {
        "symbol": "ETHUSDT",
        "min_spread": 2.0,
        "spread": 3.5,
        "spread_bps": 35.0,
        "cheap_exchange": "binance",
        "expensive_exchange": "okx",
        "long_order": {"exchange": "binance", "side": "long", "price": 100.0},
        "short_order": {"exchange": "okx", "side": "short", "price": 103.5},
        "success": True,
        "details": {},
    }
    monkeypatch.setattr(
        "app.routers.arb.execute_hedged_trade", lambda *args, **kwargs: trade_result
    )
    resp = client.post(
        "/api/arb/execute",
        json={
            "symbol": "ETHUSDT",
            "min_spread": 2.0,
            "notion_usdt": 1000.0,
            "leverage": 3.0,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "position" in body
    stored_positions = list_positions()
    assert len(stored_positions) == 1
    assert stored_positions[0]["symbol"] == "ETHUSDT"
    assert stored_positions[0]["status"] == "open"
