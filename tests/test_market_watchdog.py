import time
from types import SimpleNamespace

import pytest

from app.market.watchdog import watchdog
from app.router.smart_router import SmartRouter


@pytest.fixture(autouse=True)
def reset_watchdog() -> None:
    watchdog.ticks.clear()
    yield
    watchdog.ticks.clear()


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> SmartRouter:
    monkeypatch.setenv("DEFAULT_PROFILE", "paper")
    monkeypatch.setenv("FF_RISK_LIMITS", "0")
    monkeypatch.setenv("FF_PRETRADE_STRICT", "0")
    monkeypatch.delenv("FF_MD_WATCHDOG", raising=False)
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    state = SimpleNamespace(config=None)
    market = SimpleNamespace()
    return SmartRouter(state=state, market_data=market)


def _spy_on_register(router: SmartRouter, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    calls = [False]
    original = router._order_tracker.register_order

    def wrapped(*args, **kwargs):
        calls[0] = True
        return original(*args, **kwargs)

    monkeypatch.setattr(router._order_tracker, "register_order", wrapped)
    return calls


def test_watchdog_unknown_allows_order(
    router: SmartRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_on_register(router, monkeypatch)

    response = router.register_order(
        strategy="test",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=None,
        ts_ns=1,
        nonce=1,
    )

    assert calls[0] is True
    assert response.get("status") != "marketdata_stale"


def test_watchdog_blocks_stale_order(router: SmartRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_on_register(router, monkeypatch)
    now = int(time.time())
    watchdog.beat("binance", "BTCUSDT", ts=now - 999)

    response = router.register_order(
        strategy="test",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=None,
        ts_ns=2,
        nonce=1,
    )

    assert calls[0] is False
    assert response["status"] == "marketdata_stale"
    assert response["reason"] == "marketdata_stale"


def test_watchdog_allows_fresh_order(router: SmartRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_on_register(router, monkeypatch)
    now = int(time.time())
    watchdog.beat("binance", "BTCUSDT", ts=now)

    response = router.register_order(
        strategy="test",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=None,
        ts_ns=3,
        nonce=1,
    )

    assert calls[0] is True
    assert response.get("status") != "marketdata_stale"
