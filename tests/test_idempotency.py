from __future__ import annotations

from unittest.mock import AsyncMock

import time
import types
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.orders.idempotency import IdempoStore, make_coid
from app.orders.state import OrderState
from app.router.smart_router import SmartRouter
from app.pricing import TradeCostEstimate
from app.services import runtime
from app.utils.idem import IdempotencyCache


def test_idempotency_cache_replay() -> None:
    cache = IdempotencyCache(ttl_seconds=60)
    fingerprint = cache.build_fingerprint(
        "POST",
        "/api/ui/hold",
        b'{"foo": 1}',
        "application/json",
    )
    cache.store(
        "cache-key",
        fingerprint,
        status_code=202,
        headers=[("content-type", "application/json"), ("x-custom", "value")],
        body=b'{"detail": "ok"}',
    )
    cached = cache.get("cache-key", fingerprint)
    assert cached is not None
    assert cached.status_code == 202
    assert cached.body == b'{"detail": "ok"}'
    assert all(name.lower() != "idempotent-replay" for name, _ in cached.headers)


def test_duplicate_post_returns_cached_response(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    hold_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("app.routers.ui.hold_loop", hold_mock)

    first = client.post("/api/ui/hold", headers={"Idempotency-Key": "same-key"})
    assert first.status_code == 200
    assert hold_mock.await_count == 1

    replay = client.post("/api/ui/hold", headers={"Idempotency-Key": "same-key"})
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers.get("Idempotent-Replay") == "true"
    assert hold_mock.await_count == 1


def test_register_order_includes_cost_estimate(
    router_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.reset_for_tests()
    router, _ = router_setup
    fake_cost = TradeCostEstimate(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=Decimal("0.01"),
        price=Decimal("100"),
        taker_fee_bps=Decimal("2"),
        maker_fee_bps=Decimal("0"),
        estimated_fee=Decimal("0.02"),
        funding_rate=None,
        estimated_funding_cost=Decimal("0"),
        total_cost=Decimal("0.02"),
    )

    def fake_estimate(**_: object) -> TradeCostEstimate:
        return fake_cost

    monkeypatch.setattr("app.router.smart_router.estimate_trade_cost", fake_estimate)

    response = router.register_order(
        strategy="alpha",
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.01,
        price=100.0,
        ts_ns=1,
        nonce=1,
    )

    assert response["cost"] is fake_cost
    orders = runtime.get_execution_orders()
    matching = [
        entry for entry in orders if entry.get("client_order_id") == response["client_order_id"]
    ]
    assert matching, "execution orders should include the recorded order"
    assert matching[0]["cost"] == {
        "venue": fake_cost.venue,
        "symbol": fake_cost.symbol,
        "side": fake_cost.side,
        "qty": str(fake_cost.qty),
        "price": str(fake_cost.price),
        "taker_fee_bps": str(fake_cost.taker_fee_bps),
        "maker_fee_bps": str(fake_cost.maker_fee_bps),
        "estimated_fee": str(fake_cost.estimated_fee),
        "funding_rate": None,
        "estimated_funding_cost": str(fake_cost.estimated_funding_cost),
        "total_cost": str(fake_cost.total_cost),
    }


class _DummyMarketData:
    def __init__(self) -> None:
        now = time.time()
        self._book = {
            ("binance-um", "BTCUSDT"): {"bid": 100.0, "ask": 100.5, "ts": now},
            ("okx-perp", "ETHUSDT"): {"bid": 99.5, "ask": 100.0, "ts": now},
        }

    def top_of_book(self, venue: str, symbol: str) -> dict:
        key = (venue.lower(), symbol.upper())
        if key not in self._book:
            raise KeyError(key)
        return dict(self._book[key])


@pytest.fixture
def router_setup(monkeypatch) -> tuple[SmartRouter, IdempoStore]:
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
    monkeypatch.setenv("TEST_ONLY_ROUTER_META", "*")
    monkeypatch.setenv("TEST_ONLY_ROUTER_TICK_SIZE", "0.1")
    monkeypatch.setenv("TEST_ONLY_ROUTER_STEP_SIZE", "0.001")
    monkeypatch.delenv("TEST_ONLY_ROUTER_MIN_NOTIONAL", raising=False)
    market = _DummyMarketData()
    store = IdempoStore(ttl_seconds=60)
    monkeypatch.setattr("app.router.smart_router.get_state", lambda: state)
    monkeypatch.setattr("app.router.smart_router.get_market_data", lambda: market)
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    router = SmartRouter(idempo_store=store)
    return router, store


def test_make_coid_is_stable() -> None:
    base_args = ("alpha", "binance", "BTCUSDT", "buy", 1699999999123456789, 42)
    coid_one = make_coid(*base_args)
    coid_two = make_coid(*base_args)
    assert coid_one == coid_two
    assert len(coid_one) <= 32
    varied = make_coid("alpha", "binance", "BTCUSDT", "buy", 1699999999123456789, 43)
    assert varied != coid_one


def test_should_send_respects_expiry(monkeypatch) -> None:
    store = IdempoStore(ttl_seconds=0.01)
    coid = make_coid("beta", "okx", "ETHUSDT", "sell", 123, 1)
    assert store.should_send(coid)
    assert not store.should_send(coid)
    time.sleep(0.02)
    store.expire(coid)
    assert store.should_send(coid)


def test_router_flow_enforces_idempotency(router_setup) -> None:
    router, store = router_setup
    submission = router.register_order(
        strategy="gamma",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=5.0,
        ts_ns=1234567890,
        nonce=7,
    )
    coid = submission["client_order_id"]
    assert submission["state"] == OrderState.PENDING
    assert not store.should_send(coid)

    replay = router.register_order(
        strategy="gamma",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=5.0,
        ts_ns=1234567890,
        nonce=7,
    )
    assert replay["status"] == "idempotent_skip"
    assert replay["state"] == OrderState.PENDING

    assert router.process_order_event(client_order_id=coid, event="ack") == OrderState.ACK
    assert (
        router.process_order_event(client_order_id=coid, event="partial_fill", quantity=2.0)
        == OrderState.PARTIAL
    )
    assert (
        router.process_order_event(client_order_id=coid, event="partial_fill", quantity=1.5)
        == OrderState.PARTIAL
    )
    assert (
        router.process_order_event(client_order_id=coid, event="filled", quantity=1.5)
        == OrderState.FILLED
    )

    snapshot = router.get_order_snapshot(coid)
    assert snapshot["state"] == OrderState.FILLED
    assert pytest.approx(snapshot["filled_qty"], rel=1e-9) == 5.0
    assert not store.should_send(coid)
