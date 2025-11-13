from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.config.feature_flags as ff
from app.router import smart_router as smart_router_module
from app.router.smart_router import SmartRouter


class DummyMarketData:
    def top_of_book(self, venue: str, symbol: str) -> dict[str, float]:
        return {"bid": 100.0, "ask": 101.0, "ts": time.time()}


def _make_router(monkeypatch: pytest.MonkeyPatch, tmp_path) -> SmartRouter:
    outbox_path = tmp_path / "journal" / "outbox.jsonl"
    monkeypatch.setenv("FF_IDEMPOTENCY_OUTBOX", "1")
    monkeypatch.setenv("OUTBOX_PATH", str(outbox_path))
    monkeypatch.setenv("OUTBOX_DUPE_WINDOW_SEC", "60")
    monkeypatch.setenv("OUTBOX_RETRY_SEC", "5")
    monkeypatch.setenv("OUTBOX_ROTATE_MB", "8")
    monkeypatch.setenv("OUTBOX_FLUSH_EVERY", "1")
    monkeypatch.setenv("OUTBOX_MAX_INMEM", "1000")

    monkeypatch.setattr(smart_router_module, "get_liquidity_status", lambda: {})
    monkeypatch.setattr(smart_router_module, "get_profile", lambda: SimpleNamespace(name="test"))
    monkeypatch.setattr(smart_router_module, "metrics_write", lambda *args, **kwargs: None)
    monkeypatch.setattr(ff, "risk_limits_on", lambda: False)
    monkeypatch.setattr(ff, "md_watchdog_on", lambda: False)
    monkeypatch.setattr(ff, "pretrade_strict_on", lambda: False)

    state = SimpleNamespace(config=None)
    market_data = DummyMarketData()
    return SmartRouter(state=state, market_data=market_data)


def test_outbox_guard_and_replay(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    router = _make_router(monkeypatch, tmp_path)

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
    assert first.get("client_order_id")

    duplicate_intent = dict(base_intent)
    duplicate_intent["nonce"] = 2
    second = router.register_order(**duplicate_intent)

    assert sends["count"] == 1
    assert second.get("ok") is False
    assert second.get("reason") == "outbox-inflight"

    router.process_order_event(client_order_id=first["client_order_id"], event="ack")
    router.process_order_event(
        client_order_id=first["client_order_id"], event="filled", quantity=base_intent["qty"]
    )

    new_intent = dict(base_intent)
    new_intent["price"] = base_intent["price"] + 1.0
    new_intent["nonce"] = 3
    third = router.register_order(**new_intent)
    assert third.get("reason") != "outbox-inflight"
    assert sends["count"] == 2

    router.process_order_event(client_order_id=third["client_order_id"], event="ack")
    router.process_order_event(
        client_order_id=third["client_order_id"], event="filled", quantity=new_intent["qty"]
    )

    assert router._outbox is not None
    real_time = time.time
    stale_ts = real_time() - (router._outbox_retry_sec + 5)
    monkeypatch.setattr("app.outbox.journal.time.time", lambda: stale_ts)
    router._outbox.begin_pending(
        intent_key="stale-intent",
        order_id="stale-order",
        strategy="test",
        symbol="BTCUSDT",
        venue="binance",
        side="buy",
        qty=Decimal("0.1"),
        px=Decimal("100.0"),
    )
    monkeypatch.setattr("app.outbox.journal.time.time", real_time)

    restarted = _make_router(monkeypatch, tmp_path)
    assert restarted._outbox is not None
    candidates = list(
        restarted._outbox.iter_replay_candidates(
            now=time.time(), min_age_sec=restarted._outbox_retry_sec
        )
    )
    assert any(candidate.order_id == "stale-order" for candidate in candidates)
