from __future__ import annotations

import time
import types
from typing import Callable

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
def router_factory(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool, int], SmartRouter]:
    def factory(enable_cooldown: bool, default_ttl: int = 5) -> SmartRouter:
        for key in intent_stats:
            intent_stats[key] = 0
        monkeypatch.setenv("FF_PRETRADE_STRICT", "false")
        monkeypatch.setenv("FF_MD_WATCHDOG", "false")
        monkeypatch.setenv("FF_RISK_LIMITS", "false")
        monkeypatch.setenv("IDEMPOTENCY_WINDOW_SEC", "5")
        monkeypatch.delenv("IDEMPOTENCY_MAX_KEYS", raising=False)
        monkeypatch.setenv(
            "ROUTER_COOLDOWN_REASON_MAP",
            '{"rate_limit":8,"venue_unhealthy":10,"symbol_not_tradable":15}',
        )
        monkeypatch.setenv("ROUTER_COOLDOWN_SEC_DEFAULT", str(default_ttl))
        monkeypatch.setenv("FF_ROUTER_COOLDOWN", "1" if enable_cooldown else "0")

        clock = {"now": 1_000.0}

        def fake_time() -> float:
            return clock["now"]

        def fake_sleep(seconds: float) -> None:
            clock["now"] += float(seconds)

        monkeypatch.setattr(time, "time", fake_time)
        monkeypatch.setattr(time, "sleep", fake_sleep)
        monkeypatch.setattr("app.orders.idempotency.time.time", fake_time)
        monkeypatch.setattr("app.router.smart_router.time.time", fake_time)
        monkeypatch.setattr("app.router.cooldown.time.time", fake_time)

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
            "app.router.smart_router.SafeMode.is_active",
            classmethod(lambda cls: False),
        )

        return SmartRouter()

    return factory


def _register(
    router: SmartRouter,
    *,
    nonce: int,
    ts_ns: int,
    price: float | None = None,
) -> dict:
    return router.register_order(
        strategy="alpha",
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0 if price is None else price,
        ts_ns=ts_ns,
        nonce=nonce,
    )


def test_cooldown_blocks_until_reason_ttl(
    router_factory: Callable[[bool, int], SmartRouter]
) -> None:
    router = router_factory(enable_cooldown=True)

    first = _register(router, nonce=1, ts_ns=1_000_000, price=100.01)
    assert first["state"] == OrderState.PENDING

    router._apply_cooldown(
        venue="binance-um",
        symbol="BTCUSDT",
        strategy="alpha",
        reason="rate_limit",
    )

    blocked = _register(router, nonce=2, ts_ns=1_000_500, price=100.02)
    assert blocked["status"] == "cooldown"
    assert blocked["reason"] == "rate_limit"
    assert blocked["cooldown_remaining"] > 0

    time.sleep(9.0)

    allowed = _register(router, nonce=3, ts_ns=1_001_500, price=100.03)
    assert allowed["state"] == OrderState.PENDING


def test_cooldown_disabled(router_factory: Callable[[bool, int], SmartRouter]) -> None:
    router = router_factory(enable_cooldown=False)

    key = router._cooldown_key("binance-um", "BTCUSDT", "alpha")
    router._cooldown_registry.hit(key, seconds=10, reason="rate_limit")

    response = _register(router, nonce=1, ts_ns=1_500_000, price=100.01)
    assert response["state"] == OrderState.PENDING


def test_cooldown_default_ttl(router_factory: Callable[[bool, int], SmartRouter]) -> None:
    router = router_factory(enable_cooldown=True, default_ttl=3)

    first = _register(router, nonce=1, ts_ns=2_000_000, price=100.01)
    assert first["state"] == OrderState.PENDING

    router._apply_cooldown(
        venue="binance-um",
        symbol="BTCUSDT",
        strategy="alpha",
        reason="unknown_reason",
    )

    blocked = _register(router, nonce=2, ts_ns=2_000_500, price=100.02)
    assert blocked["status"] == "cooldown"

    time.sleep(3.5)

    allowed = _register(router, nonce=3, ts_ns=2_001_500, price=100.03)
    assert allowed["state"] == OrderState.PENDING
