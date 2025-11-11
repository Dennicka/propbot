from __future__ import annotations

import pytest

from app.services.runtime import (
    approve_resume,
    engage_safety_hold,
    get_state,
    is_hold_active,
    record_resume_request,
    reset_for_tests,
)
from positions import list_positions, reset_positions
from services import edge_guard


@pytest.fixture(autouse=True)
def _stub_liquidity(monkeypatch):
    monkeypatch.setattr(
        edge_guard.balances_monitor,
        "evaluate_balances",
        lambda: {"per_venue": {}, "liquidity_blocked": False, "reason": "ok"},
    )


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
        "app.routers.arb.can_open_new_position",
        lambda notion, leverage, **_: (True, ""),
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


def test_cross_execute_blocked_by_hold(client, monkeypatch):
    reset_positions()
    reset_for_tests()
    state = get_state()
    state.control.safe_mode = False
    engage_safety_hold("unit-test-hold", source="pytest")
    payload = {
        "symbol": "ETHUSDT",
        "min_spread": 1.0,
        "notion_usdt": 500.0,
        "leverage": 2.0,
    }
    resp = client.post("/api/arb/execute", json=payload)
    assert resp.status_code == 423
    detail = resp.json()["detail"]
    assert detail["error"] == "hold_active"
    reset_for_tests()


def test_runaway_breaker_triggers_hold(client, monkeypatch):
    monkeypatch.setenv("MAX_ORDERS_PER_MIN", "3")
    reset_positions()
    reset_for_tests()
    record_resume_request("runaway_breaker_test", requested_by="pytest")
    approve_resume(actor="pytest")
    state = get_state()
    state.control.safe_mode = False
    monkeypatch.setattr(
        "app.routers.arb.can_open_new_position",
        lambda notion, leverage, **_: (True, ""),
    )

    def fake_spread(symbol: str) -> dict:
        return {
            "symbol": symbol,
            "cheap": "binance",
            "expensive": "okx",
            "cheap_ask": 100.0,
            "cheap_mark": 100.0,
            "expensive_bid": 102.5,
            "expensive_mark": 102.5,
            "spread": 2.5,
            "spread_bps": 25.0,
        }

    monkeypatch.setattr("services.cross_exchange_arb.check_spread", fake_spread)
    monkeypatch.setattr("services.cross_exchange_arb.is_dry_run_mode", lambda: True)

    def fake_choose_venue(side: str, symbol: str, size: float) -> dict:
        if side.lower() in {"long", "buy"}:
            price = 100.0
            venue = "binance"
        else:
            price = 102.5
            venue = "okx"
        return {
            "venue": venue,
            "expected_fill_px": price,
            "fee_bps": 2,
            "liquidity_ok": True,
            "size": size,
            "expected_notional": size * price,
        }

    monkeypatch.setattr("services.cross_exchange_arb.choose_venue", fake_choose_venue)
    monkeypatch.setattr("services.cross_exchange_arb._record_execution_stat", lambda **_: None)

    payload = {
        "symbol": "ETHUSDT",
        "min_spread": 1.0,
        "notion_usdt": 250.0,
        "leverage": 2.0,
    }

    first = client.post("/api/arb/execute", json=payload)
    assert first.status_code == 200
    assert first.json()["success"] is True
    assert is_hold_active() is False
    assert is_hold_active() is False

    second = client.post("/api/arb/execute", json=payload)
    assert second.status_code == 423
    detail = second.json()["detail"]
    assert "orders_per_min_limit_exceeded" in str(detail.get("error", detail))
    assert is_hold_active() is True
    reset_for_tests()
    reset_positions()
