from types import SimpleNamespace

import pytest

from app.market.watchdog import watchdog
from app.router.smart_router import SmartRouter


@pytest.fixture(autouse=True)
def reset_watchdog() -> None:
    watchdog.ticks.clear()
    watchdog.staleness_samples.clear()
    watchdog.cooldown_until.clear()
    yield
    watchdog.ticks.clear()
    watchdog.staleness_samples.clear()
    watchdog.cooldown_until.clear()


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> SmartRouter:
    monkeypatch.setenv("DEFAULT_PROFILE", "paper")
    monkeypatch.setenv("FF_RISK_LIMITS", "0")
    monkeypatch.setenv("FF_PRETRADE_STRICT", "0")
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


def test_staleness_gate_blocks_and_recovers(
    router: SmartRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FF_MD_WATCHDOG", "1")
    monkeypatch.setenv("STALE_P95_LIMIT_MS", "1500")
    monkeypatch.setenv("STALE_GATE_COOLDOWN_S", "10")

    current_time = [1_000_000.0]

    def fake_time() -> float:
        return current_time[0]

    monkeypatch.setattr("app.market.watchdog.time.time", fake_time)

    calls = _spy_on_register(router, monkeypatch)

    watchdog.beat("binance", "BTCUSDT", ts=current_time[0] - 10)

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

    assert calls[0] is False
    assert response["status"] == "marketdata_stale"
    assert response.get("gate_reason") == "md_stale_p95"
    assert watchdog.cooldown_active("binance") is True

    for _ in range(25):
        current_time[0] += 1
        watchdog.beat("binance", "BTCUSDT", ts=current_time[0])
        watchdog.staleness_ms("binance", "BTCUSDT")

    current_time[0] += watchdog.cooldown_seconds() + 1
    watchdog.beat("binance", "BTCUSDT", ts=current_time[0])
    watchdog.staleness_ms("binance", "BTCUSDT")

    assert watchdog.cooldown_active("binance") is False
    assert watchdog.get_p95("binance") <= watchdog.stale_p95_limit_ms()

    calls[0] = False
    response_ok = router.register_order(
        strategy="test",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=None,
        ts_ns=2,
        nonce=2,
    )

    assert calls[0] is True
    assert response_ok.get("status") != "marketdata_stale"
    assert response_ok.get("gate_reason") is None
