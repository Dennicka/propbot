from __future__ import annotations

from fastapi.testclient import TestClient

from app.market.streams.base_ws import WsState
from app.services.market_ws import get_orderbook_store, reset_for_tests as reset_market_store
from app.metrics.market_ws import reset_for_tests as reset_market_metrics


def setup_module(_: object) -> None:
    reset_market_store()
    reset_market_metrics()


def test_market_status_reports_resync_and_staleness(client: TestClient) -> None:
    reset_market_store()
    reset_market_metrics()
    store = get_orderbook_store()
    store.apply_snapshot(
        venue="binance",
        symbol="BTCUSDT",
        bids=[(100.0, 1.0)],
        asks=[(101.0, 1.0)],
        last_seq=10,
        ts_ms=None,
    )
    store.record_resync("binance", "BTCUSDT", "gap")
    store.set_state("binance", "BTCUSDT", WsState.CONNECTED, reason="resync_complete")
    response = client.get("/api/ui/market_status")
    payload = response.json()
    assert response.status_code == 200
    assert "markets" in payload
    entry = next(item for item in payload["markets"] if item["symbol"] == "BTCUSDT")
    assert entry["resyncs"] >= 1
    assert entry["state"] == WsState.CONNECTED.value
    assert entry["last_reason"]
    assert entry["staleness_s"] >= 0.0
