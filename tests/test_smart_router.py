import time
import types

import pytest

from app.router.smart_router import SmartRouter


class StubMarketData:
    def __init__(self, books):
        self._books = books

    def top_of_book(self, venue: str, symbol: str) -> dict:
        key = (venue.lower(), symbol.upper())
        if key not in self._books:
            raise KeyError(key)
        payload = dict(self._books[key])
        payload.setdefault("ts", time.time())
        return payload


@pytest.fixture
def base_setup(monkeypatch):
    state = types.SimpleNamespace(
        control=types.SimpleNamespace(
            post_only=False,
            taker_fee_bps_binance=2,
            taker_fee_bps_okx=2,
            default_taker_fee_bps=2,
        ),
        config=types.SimpleNamespace(
            data=types.SimpleNamespace(
                tca=types.SimpleNamespace(
                    horizon_min=1.0,
                    impact=types.SimpleNamespace(k=0.0),
                    tiers={},
                ),
                derivatives=types.SimpleNamespace(
                    arbitrage=types.SimpleNamespace(prefer_maker=False),
                    fees=types.SimpleNamespace(manual={}),
                ),
            )
        ),
        derivatives=types.SimpleNamespace(venues={}),
    )
    now = time.time()
    market = StubMarketData(
        {
            ("binance-um", "BTCUSDT"): {"bid": 100.0, "ask": 101.0, "ts": now},
            ("okx-perp", "BTCUSDT"): {"bid": 100.1, "ask": 101.1, "ts": now - 0.2},
        }
    )
    monkeypatch.setenv("FEATURE_SMART_ROUTER", "1")
    monkeypatch.setenv("SMART_ROUTER_LATENCY_BPS_PER_MS", "0.01")
    monkeypatch.setattr("app.router.smart_router.get_state", lambda: state)
    monkeypatch.setattr("app.router.smart_router.get_market_data", lambda: market)
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    return state, market


def test_smart_router_prefers_liquidity_and_latency(base_setup, monkeypatch):
    state, _ = base_setup
    state.config.data.derivatives.fees.manual = {}
    router = SmartRouter()
    liquidity = {"binance-um": 5_000_000.0, "okx-perp": 25_000.0}
    rest = {"binance-um": 12.0, "okx-perp": 220.0}
    ws = {"binance-um": 5.0, "okx-perp": 260.0}

    best, scores = router.choose(
        ["binance-um", "okx-perp"],
        side="buy",
        qty=1.0,
        symbol="BTCUSDT",
        book_liquidity_usdt=liquidity,
        rest_latency_ms=rest,
        ws_latency_ms=ws,
    )

    assert best == "binance-um"
    assert scores["binance-um"]["score"] < scores["okx-perp"]["score"]


def test_smart_router_penalises_latency_against_rebate(base_setup, monkeypatch):
    state, _ = base_setup
    state.config.data.derivatives.fees.manual = {
        "binance-um": {"maker_bps": 0.5, "taker_bps": 2.5, "vip_rebate_bps": 0.0},
        "okx-perp": {"maker_bps": 0.0, "taker_bps": -2.0, "vip_rebate_bps": 0.5},
    }
    monkeypatch.setenv("SMART_ROUTER_LATENCY_BPS_PER_MS", "0.05")
    router = SmartRouter()
    liquidity = {"binance-um": 2_000_000.0, "okx-perp": 2_000_000.0}
    rest = {"binance-um": 30.0, "okx-perp": 950.0}
    ws = {"binance-um": 20.0, "okx-perp": 980.0}

    best, scores = router.choose(
        ["binance-um", "okx-perp"],
        side="sell",
        qty=1.5,
        symbol="BTCUSDT",
        book_liquidity_usdt=liquidity,
        rest_latency_ms=rest,
        ws_latency_ms=ws,
    )

    assert best == "binance-um"
    assert scores["okx-perp"]["score"] > scores["binance-um"]["score"]
    state.config.data.derivatives.fees.manual = {}


def test_smart_router_tiebreaks_by_canonical_name(base_setup, monkeypatch):
    state, _ = base_setup
    state.config.data.derivatives.fees.manual = {
        "binance-um": {"maker_bps": 1.0, "taker_bps": 1.0, "vip_rebate_bps": 0.0},
        "okx-perp": {"maker_bps": 1.0, "taker_bps": 1.0, "vip_rebate_bps": 0.0},
    }
    monkeypatch.setenv("SMART_ROUTER_LATENCY_BPS_PER_MS", "0.0")
    router = SmartRouter()
    liquidity = {"binance-um": 1_000_000.0, "okx-perp": 1_000_000.0}
    rest = {"binance-um": 100.0, "okx-perp": 100.0}
    ws = {"binance-um": 100.0, "okx-perp": 100.0}

    best, _ = router.choose(
        ["binance-um", "okx-perp"],
        side="buy",
        qty=1.0,
        symbol="BTCUSDT",
        book_liquidity_usdt=liquidity,
        rest_latency_ms=rest,
        ws_latency_ms=ws,
    )

    assert best == "binance-um"
    state.config.data.derivatives.fees.manual = {}
