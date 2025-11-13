import time
from types import SimpleNamespace

import pytest

import app.config.feature_flags as ff
from app.orders.outbox import Outbox
from app.router import smart_router as smart_router_module
from app.router.smart_router import SmartRouter


class DummyMarketData:
    def top_of_book(self, venue: str, symbol: str) -> dict[str, float]:
        return {"bid": 100.0, "ask": 101.0, "ts": time.time()}


def _make_router(monkeypatch: pytest.MonkeyPatch) -> SmartRouter:
    monkeypatch.setattr(smart_router_module, "get_liquidity_status", lambda: {})
    monkeypatch.setattr(ff, "risk_limits_on", lambda: False)
    monkeypatch.setattr(ff, "md_watchdog_on", lambda: False)
    monkeypatch.setattr(ff, "pretrade_strict_on", lambda: False)
    state = SimpleNamespace(config=None)
    market_data = DummyMarketData()
    return SmartRouter(state=state, market_data=market_data)


def test_should_send_blocks_duplicates() -> None:
    outbox = Outbox()
    key = "order-key"
    assert outbox.should_send(key) is True
    assert outbox.should_send(key) is False
    assert outbox.stats["skip_duplicate"] == 1


def test_mark_acked_and_terminal_cleanup() -> None:
    outbox = Outbox()
    key = "intent"
    assert outbox.should_send(key) is True
    outbox.mark_acked(key)
    outbox.mark_terminal(key)
    assert outbox.should_send(key) is True


def test_router_integration_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FF_IDEMPOTENCY_OUTBOX", "1")
    router = _make_router(monkeypatch)

    sends = {"count": 0}
    original_register = router._order_tracker.register_order

    def _wrapped_register(*args, **kwargs):
        sends["count"] += 1
        return original_register(*args, **kwargs)

    monkeypatch.setattr(router._order_tracker, "register_order", _wrapped_register)

    base_intent = dict(
        strategy="test",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts_ns=123456789,
        nonce=1,
    )

    first = router.register_order(**base_intent)
    duplicate_intent = dict(base_intent)
    duplicate_intent["nonce"] = 2
    second = router.register_order(**duplicate_intent)

    assert sends["count"] == 1
    assert second["status"] == "duplicate_intent"

    router.process_order_event(client_order_id=first["client_order_id"], event="reject")

    retry_intent = dict(base_intent)
    retry_intent["nonce"] = 3
    third = router.register_order(**retry_intent)

    assert sends["count"] == 2
    assert third.get("status") != "duplicate_intent"
