from __future__ import annotations

from app.services.runtime import get_state


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
    state = get_state()
    state.control.safe_mode = False
    monkeypatch.setattr(
        "app.routers.arb.can_open_new_position", lambda notion, leverage: (True, "")
    )
    trade_result = {
        "symbol": "ETHUSDT",
        "min_spread": 2.0,
        "spread": 3.5,
        "cheap_exchange": "binance",
        "expensive_exchange": "okx",
        "long_order": {"exchange": "binance", "side": "long"},
        "short_order": {"exchange": "okx", "side": "short"},
        "success": True,
        "details": {},
    }
    monkeypatch.setattr(
        "app.routers.arb.execute_hedged_trade", lambda *args, **kwargs: trade_result
    )
    captured = {}

    def fake_register(payload):
        captured["payload"] = payload

    monkeypatch.setattr("app.routers.arb.register_position", fake_register)

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
    assert captured["payload"]["symbol"] == "ETHUSDT"
    assert len(captured["payload"]["legs"]) == 2
