import time
import types
from unittest.mock import MagicMock

import pytest

from app.orders.idempotency import stats as intent_stats
from app.orders.state import OrderState
from app.router.smart_router import SmartRouter


class _DummyMarketData:
    def __init__(self) -> None:
        now = time.time()
        self._book = {
            ("binance-um", "BTCUSDT"): {"bid": 100.0, "ask": 100.5, "ts": now},
        }

    def top_of_book(self, venue: str, symbol: str) -> dict:
        key = (venue.lower(), symbol.upper())
        if key not in self._book:
            raise KeyError(key)
        return dict(self._book[key])


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> SmartRouter:
    for key in intent_stats:
        intent_stats[key] = 0
    monkeypatch.setenv("FF_PRETRADE_STRICT", "false")
    monkeypatch.setenv("FF_MD_WATCHDOG", "false")
    monkeypatch.setenv("FF_RISK_LIMITS", "false")
    monkeypatch.setenv("IDEMPOTENCY_WINDOW_SEC", "5")
    monkeypatch.delenv("IDEMPOTENCY_MAX_KEYS", raising=False)

    clock = {"now": 1_000.0}

    def fake_time() -> float:
        return clock["now"]

    def fake_sleep(seconds: float) -> None:
        clock["now"] += float(seconds)

    monkeypatch.setattr(time, "time", fake_time)
    monkeypatch.setattr(time, "sleep", fake_sleep)
    monkeypatch.setattr("app.orders.idempotency.time.time", fake_time)
    monkeypatch.setattr("app.router.smart_router.time.time", fake_time)

    state = types.SimpleNamespace(
        control=types.SimpleNamespace(
            post_only=False,
            taker_fee_bps_binance=2.0,
            taker_fee_bps_okx=2.0,
            default_taker_fee_bps=2.0,
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
    market = _DummyMarketData()

    monkeypatch.setattr("app.router.smart_router.get_state", lambda: state)
    monkeypatch.setattr("app.router.smart_router.get_market_data", lambda: market)
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    monkeypatch.setattr(
        "app.router.smart_router.get_profile",
        lambda: types.SimpleNamespace(name="paper", allow_trading=True, strict_flags=False),
    )
    monkeypatch.setattr(
        "app.router.smart_router.SafeMode.is_active", classmethod(lambda cls: False)
    )

    return SmartRouter()


def test_router_intent_window_duplicate(
    monkeypatch: pytest.MonkeyPatch, router: SmartRouter
) -> None:
    register_spy = MagicMock(wraps=router._order_tracker.register_order)
    monkeypatch.setattr(router._order_tracker, "register_order", register_spy)

    first = router.register_order(
        strategy="alpha",
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts_ns=1_000_000,
        nonce=1,
    )
    assert first["state"] == OrderState.PENDING
    assert register_spy.call_count == 1

    second = router.register_order(
        strategy="alpha",
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts_ns=1_000_500,
        nonce=2,
    )
    assert second["status"] == "pretrade_rejected"
    assert second["reason"] == "dupe-intent"
    assert register_spy.call_count == 1
    assert intent_stats["dupe"] >= 1

    time.sleep(6.0)
    removed_ttl, removed_size = router._intent_window.cleanup()
    assert removed_ttl == 1
    assert removed_size == 0

    third = router.register_order(
        strategy="alpha",
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts_ns=1_001_500,
        nonce=3,
    )
    assert third["state"] == OrderState.PENDING
    assert register_spy.call_count == 2
    assert intent_stats["touch"] >= 2
