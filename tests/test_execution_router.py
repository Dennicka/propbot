import time
import types

import pytest

from services import execution_router


class DummyClient:
    def __init__(self, mark_price: float, available: float) -> None:
        self.mark_price = mark_price
        self.available = available

    def get_mark_price(self, symbol: str) -> dict:
        return {"symbol": symbol, "mark_price": float(self.mark_price)}

    def get_account_limits(self) -> dict:
        return {"available_balance": float(self.available)}


class DummyMarketData:
    def __init__(self) -> None:
        self.books: dict[str, dict[str, float]] = {}

    def top_of_book(self, venue: str, symbol: str) -> dict[str, float]:
        book = self.books.get(venue)
        if book is None:
            base = venue.split("-", 1)[0]
            book = self.books.get(base, {})
        return {
            "bid": float(book.get("bid", 0.0)),
            "ask": float(book.get("ask", 0.0)),
            "ts": float(book.get("ts", time.time())),
        }


@pytest.fixture(autouse=True)
def _patch_runtime(monkeypatch):
    state = types.SimpleNamespace(
        control=types.SimpleNamespace(
            taker_fee_bps_binance=2,
            taker_fee_bps_okx=5,
        )
    )
    monkeypatch.setattr(execution_router, "get_state", lambda: state)
    monkeypatch.setattr(execution_router, "is_dry_run_mode", lambda: False)
    monkeypatch.setattr(execution_router, "get_liquidity_status", lambda: {})
    market_data = DummyMarketData()
    monkeypatch.setattr(execution_router, "get_market_data", lambda: market_data)
    return market_data


def test_choose_venue_prefers_best_effective_price(monkeypatch, _patch_runtime):
    market_data = _patch_runtime
    market_data.books = {
        "binance": {"bid": 100.0, "ask": 100.0, "ts": time.time()},
        "okx": {"bid": 99.4, "ask": 99.4, "ts": time.time()},
    }
    binance = DummyClient(mark_price=100.0, available=10_000.0)
    okx = DummyClient(mark_price=99.4, available=10_000.0)
    monkeypatch.setattr(
        execution_router,
        "_CLIENTS",
        {
            "binance": execution_router._VenueAdapter("binance", binance),
            "okx": execution_router._VenueAdapter("okx", okx),
        },
    )

    result = execution_router.choose_venue("long", "BTCUSDT", 1.0)

    assert result["venue"] == "okx"
    assert pytest.approx(result["expected_fill_px"]) == 99.4
    assert result["liquidity_ok"] is True


def test_choose_venue_marks_insufficient_liquidity(monkeypatch, _patch_runtime):
    market_data = _patch_runtime
    market_data.books = {
        "binance": {"bid": 100.0, "ask": 100.5, "ts": time.time()},
        "okx": {"bid": 101.0, "ask": 101.5, "ts": time.time()},
    }
    binance = DummyClient(mark_price=100.0, available=500.0)
    okx = DummyClient(mark_price=101.0, available=5.0)
    monkeypatch.setattr(
        execution_router,
        "_CLIENTS",
        {
            "binance": execution_router._VenueAdapter("binance", binance),
            "okx": execution_router._VenueAdapter("okx", okx),
        },
    )

    result = execution_router.choose_venue("short", "ETHUSDT", 2.0)

    assert result["venue"] == "binance"
    assert result["liquidity_ok"] is True

    result_large = execution_router.choose_venue("short", "ETHUSDT", 100.0)
    assert result_large["liquidity_ok"] is False


def test_choose_venue_scoring_prefers_best_ask(monkeypatch, _patch_runtime):
    market_data = _patch_runtime
    market_data.books = {
        "binance": {"bid": 99.0, "ask": 100.0, "ts": time.time()},
        "okx": {"bid": 99.5, "ask": 101.0, "ts": time.time()},
    }
    binance = DummyClient(mark_price=100.0, available=10_000.0)
    okx = DummyClient(mark_price=101.0, available=10_000.0)
    monkeypatch.setattr(
        execution_router,
        "_CLIENTS",
        {
            "binance": execution_router._VenueAdapter("binance", binance),
            "okx": execution_router._VenueAdapter("okx", okx),
        },
    )

    result = execution_router.choose_venue("buy", "BTCUSDT", 2.0)

    expected = execution_router.VENUE_ALIASES.get("binance", "binance")
    assert result["canonical_venue"] == expected
    scoring = result.get("smart_router", {}).get("scoring", {})
    assert scoring.get("best") == expected


def test_choose_venue_scoring_prefers_best_bid(monkeypatch, _patch_runtime):
    market_data = _patch_runtime
    market_data.books = {
        "binance": {"bid": 110.0, "ask": 111.0, "ts": time.time()},
        "okx": {"bid": 108.0, "ask": 109.0, "ts": time.time()},
    }
    binance = DummyClient(mark_price=110.0, available=10_000.0)
    okx = DummyClient(mark_price=108.0, available=10_000.0)
    monkeypatch.setattr(
        execution_router,
        "_CLIENTS",
        {
            "binance": execution_router._VenueAdapter("binance", binance),
            "okx": execution_router._VenueAdapter("okx", okx),
        },
    )

    result = execution_router.choose_venue("sell", "BTCUSDT", 1.5)

    expected = execution_router.VENUE_ALIASES.get("binance", "binance")
    assert result["canonical_venue"] == expected
    scoring = result.get("smart_router", {}).get("scoring", {})
    assert scoring.get("best") == expected
