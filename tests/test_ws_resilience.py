from __future__ import annotations

import math
from typing import Dict, List

import pytest

from app.exchanges.binance_native import BinanceOrderBookStream
from app.exchanges.okx_native import OkxOrderBookStream
from app.market.orderbook.book_store import DiffEvent, OrderBookStore
from app.market.streams.base_ws import BackoffPolicy, HeartbeatMonitor, WsConnector, WsState
from app.metrics.market_ws import reset_for_tests as reset_ws_metrics
from app.metrics.observability import register_slo_metrics, reset_for_tests as reset_obs_metrics
from app.services.market_ws import get_orderbook_store, reset_for_tests as reset_store


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    reset_store()
    reset_ws_metrics()
    reset_obs_metrics()


def _clock_factory() -> tuple[callable, callable[[float], None]]:
    current = {"now": 0.0}

    def now() -> float:
        return current["now"]

    def advance(value: float) -> None:
        current["now"] = value

    return now, advance


def _make_connector(venue: str, now) -> tuple[WsConnector, List[str]]:
    heartbeat = HeartbeatMonitor(timeout=5.0, clock=now)
    backoff = BackoffPolicy(
        base=0.25, maximum=30.0, stable_window=60.0, jitter=lambda a, b: a, clock=now
    )
    reasons: List[str] = []
    connector = WsConnector(
        venue=venue, heartbeat=heartbeat, backoff=backoff, reconnect=reasons.append
    )
    return connector, reasons


def test_binance_gap_triggers_resync_and_recovers() -> None:
    now, advance = _clock_factory()
    connector, reasons = _make_connector("binance", now)
    store = get_orderbook_store()
    snapshots = [
        {"lastUpdateId": 100, "bids": [[100.0, 1.0]], "asks": [[101.0, 2.0]], "ts_ms": 1000},
        {"lastUpdateId": 200, "bids": [[101.5, 3.0]], "asks": [[102.5, 4.0]], "ts_ms": 2000},
    ]
    resync_iter = iter(snapshots[1:])

    def fetch_snapshot(symbol: str) -> Dict[str, object]:
        try:
            return next(resync_iter)
        except StopIteration:
            return snapshots[-1]

    stream = BinanceOrderBookStream(
        venue="binance",
        orderbook=store,
        connector=connector,
        snapshot_fetcher=fetch_snapshot,
    )
    stream.handle_snapshot("BTCUSDT", snapshots[0])

    good_diff: DiffEvent = {
        "symbol": "BTCUSDT",
        "bids": [(100.5, 1.0)],
        "asks": [(101.5, 2.0)],
        "seq_from": 101,
        "seq_to": 101,
        "ts_ms": 1100,
    }
    stream.handle_diff(good_diff)

    gap_diff: DiffEvent = {
        "symbol": "BTCUSDT",
        "bids": [(101.0, 2.5)],
        "asks": [(102.0, 1.0)],
        "seq_from": 103,
        "seq_to": 103,
        "ts_ms": 1200,
    }
    stream.handle_diff(gap_diff)

    status = store.status_snapshot()
    assert status[0]["resyncs"] == 1
    assert status[0]["state"] == WsState.CONNECTED.value
    assert reasons == []
    top = store.get_top_of_book("binance", "BTCUSDT")
    assert math.isclose(top["bid"], 101.5)
    assert math.isclose(top["ask"], 102.5)


def test_okx_seq_gap_resubscribe() -> None:
    now, advance = _clock_factory()
    connector, _ = _make_connector("okx", now)
    store = get_orderbook_store()
    snapshots = [
        {"seq": 10, "bids": [[200.0, 1.0]], "asks": [[201.0, 1.5]], "ts_ms": 1000},
        {"seq": 50, "bids": [[202.0, 1.0]], "asks": [[203.0, 1.5]], "ts_ms": 2000},
    ]
    resync_iter = iter(snapshots[1:])

    def fetch(symbol: str) -> Dict[str, object]:
        try:
            return next(resync_iter)
        except StopIteration:
            return snapshots[-1]

    stream = OkxOrderBookStream(
        venue="okx",
        orderbook=store,
        connector=connector,
        snapshot_fetcher=fetch,
    )
    symbol = "BTC-USDT-SWAP"
    stream.handle_snapshot(symbol, snapshots[0])

    ok_diff: DiffEvent = {
        "symbol": symbol,
        "bids": [(200.5, 1.0)],
        "asks": [(201.5, 1.0)],
        "seq_from": 11,
        "seq_to": 11,
        "ts_ms": 1100,
    }
    stream.handle_diff(ok_diff)

    gap_diff: DiffEvent = {
        "symbol": symbol,
        "bids": [(201.0, 1.0)],
        "asks": [(202.0, 1.0)],
        "seq_from": 13,
        "seq_to": 13,
        "ts_ms": 1200,
    }
    stream.handle_diff(gap_diff)

    post_resync_diff: DiffEvent = {
        "symbol": symbol,
        "bids": [(202.5, 1.0)],
        "asks": [(203.5, 1.0)],
        "seq_from": 51,
        "seq_to": 51,
        "ts_ms": 2100,
    }
    stream.handle_diff(post_resync_diff)

    status = store.status_snapshot()
    entry = next(item for item in status if item["symbol"] == symbol)
    assert entry["resyncs"] == 1
    assert entry["last_seq"] == 51


def test_heartbeat_timeout_reconnects() -> None:
    now, advance = _clock_factory()
    connector, reasons = _make_connector("binance", now)
    connector.on_open()
    connector.on_message(ts=now())
    advance(10.0)
    connector.check_heartbeat()
    assert reasons == ["heartbeat_timeout"]
    assert connector.state == WsState.DOWN


def test_backoff_grows_and_resets_after_stability() -> None:
    now, advance = _clock_factory()
    policy = BackoffPolicy(
        base=0.25, maximum=5.0, stable_window=60.0, jitter=lambda a, b: a, clock=now
    )
    delays = [policy.next_delay(), policy.next_delay(), policy.next_delay()]
    assert delays == [0.25, 0.5, 1.0]
    advance(100.0)
    policy.record_success()
    advance(200.0)
    policy.record_success()
    assert policy.next_delay() == 0.25


def test_staleness_metric_exposed() -> None:
    now, advance = _clock_factory()
    store = OrderBookStore(now=now)
    register_slo_metrics()
    store.apply_snapshot(
        venue="binance",
        symbol="BTCUSDT",
        bids=[(100.0, 1.0)],
        asks=[(101.0, 1.0)],
        last_seq=1,
        ts_ms=0,
    )
    advance(5.0)
    staleness = store.get_staleness_s("binance", "BTCUSDT")
    assert pytest.approx(staleness, rel=1e-6) == 5.0
