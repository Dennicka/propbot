import time
from types import SimpleNamespace

import pytest

from app.config.profile import TradingProfile
from app.health.aggregator import get_agg
from app.orders.state import OrderState
from app.router import smart_router as smart_router_module
from app.router.smart_router import SmartRouter
from app.services.safe_mode import SafeMode


class DummyMarketData:
    def top_of_book(self, venue: str, symbol: str) -> dict[str, float]:
        return {"bid": 100.0, "ask": 101.0, "ts": time.time()}


def _register(router: SmartRouter, **overrides: object) -> dict[str, object]:
    intent = {
        "strategy": "smoke",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "side": "buy",
        "qty": 0.001,
        "price": 50000.0,
        "ts_ns": 1,
        "nonce": 1,
    }
    intent.update(overrides)
    return router.register_order(**intent)


@pytest.fixture(autouse=True)
def _reset_aggregator() -> None:
    agg = get_agg()
    previous_required = agg.required
    previous_ttl = agg.ttl_seconds
    agg.clear()
    yield
    agg.clear()
    agg.configure(ttl_seconds=previous_ttl, required=previous_required)


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> SmartRouter:
    monkeypatch.setenv("FF_READINESS_AGG_GUARD", "1")
    monkeypatch.setenv("READINESS_REQUIRED", "market,recon")
    monkeypatch.setenv("READINESS_TTL_SEC", "30")
    monkeypatch.setenv("FF_RISK_LIMITS", "0")
    monkeypatch.setenv("FF_PRETRADE_STRICT", "0")
    monkeypatch.setenv("FF_MD_WATCHDOG", "0")
    monkeypatch.setenv("FF_ROUTER_COOLDOWN", "0")
    monkeypatch.setenv("FF_IDEMPOTENCY_OUTBOX", "0")
    monkeypatch.setenv("FF_ORDER_TIMEOUTS", "0")
    monkeypatch.setenv("TEST_ONLY_ROUTER_META", "*")
    monkeypatch.setenv("TEST_ONLY_ROUTER_TICK_SIZE", "0.1")
    monkeypatch.setenv("TEST_ONLY_ROUTER_STEP_SIZE", "0.001")
    monkeypatch.delenv("TEST_ONLY_ROUTER_MIN_NOTIONAL", raising=False)
    monkeypatch.setenv("EXEC_PROFILE", "paper")
    monkeypatch.setenv("READINESS_OK", "1")
    monkeypatch.setenv("LIVE_CONFIRM", "I_UNDERSTAND")

    SafeMode.set(False)

    profile = TradingProfile(
        name="paper",
        allow_trading=True,
        strict_flags=False,
        is_canary=False,
        display_name="paper",
    )
    liquidity = {"per_venue": {"binance": {"available_balance": 1000.0}}}
    state = SimpleNamespace(config=None)
    market_data = DummyMarketData()

    monkeypatch.setattr(smart_router_module, "get_state", lambda: state)
    monkeypatch.setattr(smart_router_module, "get_market_data", lambda: market_data)
    monkeypatch.setattr(smart_router_module, "get_liquidity_status", lambda: liquidity)
    monkeypatch.setattr(smart_router_module, "get_profile", lambda: profile)
    monkeypatch.setattr(smart_router_module.ff, "risk_limits_on", lambda: False)
    monkeypatch.setattr(smart_router_module.ff, "pretrade_strict_on", lambda: False)
    monkeypatch.setattr(smart_router_module.ff, "md_watchdog_on", lambda: False)

    router_instance = SmartRouter(state=state, market_data=market_data)
    get_agg().clear()
    try:
        yield router_instance
    finally:
        SafeMode.set(False)


def test_guard_blocks_when_signals_missing(router: SmartRouter) -> None:
    response = _register(router)

    assert response["ok"] is False
    assert response["reason"] == "readiness-agg"
    assert response["detail"] == "readiness-missing:market,recon"
    assert response.get("cost") is None
    assert response.get("strategy_id") == "smoke"


def test_guard_allows_when_signals_healthy(router: SmartRouter) -> None:
    agg = get_agg()
    agg.set("market", True)
    agg.set("recon", True)

    response = _register(router)

    assert response["state"] == OrderState.PENDING
    assert response["client_order_id"]


def test_guard_blocks_when_signal_bad(router: SmartRouter) -> None:
    agg = get_agg()
    agg.set("market", True)
    agg.set("recon", False, reason="lag")

    response = _register(router)

    assert response["ok"] is False
    assert response["reason"] == "readiness-agg"
    assert response["detail"] == "readiness-bad:recon:lag"
    assert response.get("cost") is None
    assert response.get("strategy_id") == "smoke"
