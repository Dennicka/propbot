from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import ledger


def _record(level: str, code: str, venue: str, symbol: str, message: str) -> None:
    ledger.record_event(level=level, code=code, payload={"venue": venue, "symbol": symbol, "message": message})


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_events_endpoint_basic_filters(client, order: str) -> None:
    ledger.reset()
    _record("INFO", "alpha", "binance-um", "BTCUSDT", "Alpha ready")
    _record("WARNING", "beta", "okx-perp", "ETHUSDT", "Beta warning")
    _record("ERROR", "gamma", "binance-um", "BTCUSDT", "Gamma failure")

    params = {"order": order, "limit": 5}
    resp = client.get("/api/ui/events", params=params)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 3
    assert payload["order"] == order
    assert len(payload["items"]) == 3
    codes = [item["code"] for item in payload["items"]]
    if order == "desc":
        assert codes[0] == "gamma"
    else:
        assert codes[0] == "alpha"

    venue_resp = client.get("/api/ui/events", params={"venue": "binance-um"})
    assert venue_resp.status_code == 200
    venues = {item["venue"] for item in venue_resp.json()["items"]}
    assert venues == {"binance-um"}

    search_resp = client.get("/api/ui/events", params={"search": "failure"})
    assert search_resp.status_code == 200
    messages = [item["message"].lower() for item in search_resp.json()["items"]]
    assert messages == ["gamma failure"]

    level_resp = client.get("/api/ui/events", params={"level": "warning"})
    assert level_resp.status_code == 200
    levels = {item["level"] for item in level_resp.json()["items"]}
    assert levels == {"WARNING"}


def test_events_endpoint_validation(client) -> None:
    ledger.reset()
    _record("INFO", "alpha", "binance-um", "BTCUSDT", "Alpha ready")

    bad_limit = client.get("/api/ui/events", params={"limit": 5001})
    assert bad_limit.status_code == 422

    bad_level = client.get("/api/ui/events", params={"level": "fatal"})
    assert bad_level.status_code == 422

    bad_order = client.get("/api/ui/events", params={"order": "sideways"})
    assert bad_order.status_code == 422

    since = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    too_wide = client.get("/api/ui/events", params={"since": since, "until": until})
    assert too_wide.status_code == 422

    window_ok = client.get(
        "/api/ui/events",
        params={
            "since": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "until": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert window_ok.status_code == 200
