from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.orders.state import OrderState
from app.router import smart_router as smart_router_module
from app.router.smart_router import SmartRouter
from app.services import runtime as runtime_module


class DummyMarketData:
    def top_of_book(self, venue: str, symbol: str) -> dict[str, float]:
        return {"bid": 0.0, "ask": 0.0, "ts": time.time()}


@pytest.fixture(autouse=True)
def _reset_runtime_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "_PROFILE", None, raising=False)


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> SmartRouter:
    monkeypatch.setattr(smart_router_module, "get_liquidity_status", lambda: {})
    state = SimpleNamespace(config=None)
    market_data = DummyMarketData()
    return SmartRouter(state=state, market_data=market_data)


def _register(router: SmartRouter) -> dict[str, object]:
    return router.register_order(
        strategy="test",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=None,
        ts_ns=123456789,
        nonce=1,
    )


def test_live_guard_missing_confirmation(monkeypatch: pytest.MonkeyPatch, router: SmartRouter):
    monkeypatch.setenv("EXEC_PROFILE", "live")
    monkeypatch.delenv("LIVE_CONFIRM", raising=False)
    monkeypatch.setenv("READINESS_OK", "1")

    result = _register(router)

    assert result["status"] == "live-confirm-missing"
    assert result["reason"] == "live-confirm-missing"


def test_live_guard_missing_readiness(monkeypatch: pytest.MonkeyPatch, router: SmartRouter):
    monkeypatch.setenv("EXEC_PROFILE", "live")
    monkeypatch.setenv("LIVE_CONFIRM", "I_UNDERSTAND")
    monkeypatch.delenv("READINESS_OK", raising=False)

    result = _register(router)

    assert result["status"] == "live-readiness-not-ok"
    assert result["reason"] == "live-readiness-not-ok"


def test_live_guard_allows_when_confirmed(monkeypatch: pytest.MonkeyPatch, router: SmartRouter):
    monkeypatch.setenv("EXEC_PROFILE", "live")
    monkeypatch.setenv("LIVE_CONFIRM", "I_UNDERSTAND")
    monkeypatch.setenv("READINESS_OK", "1")

    result = _register(router)

    assert result["state"] == OrderState.PENDING
    assert result["client_order_id"]
    assert "status" not in result or result["status"] != "live-confirm-missing"
