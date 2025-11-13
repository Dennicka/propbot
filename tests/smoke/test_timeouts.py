from __future__ import annotations

import logging
import types
from typing import Callable

import pytest

from app.orders.idempotency import IdempoStore
from app.orders.state import OrderState
from app.router.smart_router import SmartRouter


class _DummyMarketData:
    def __init__(self) -> None:
        now = 0.0
        self._book = {
            ("binance-um", "BTCUSDT"): {"bid": 100.0, "ask": 100.5, "ts": now},
            ("okx-perp", "ETHUSDT"): {"bid": 99.5, "ask": 100.0, "ts": now},
        }

    def top_of_book(self, venue: str, symbol: str) -> dict:
        key = (venue.lower(), symbol.upper())
        if key not in self._book:
            raise KeyError(key)
        return dict(self._book[key])


class _TimeController:
    def __init__(self, start: float) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def time(self) -> float:
        return self._now

    def time_ns(self) -> int:
        return int(self._now * 1_000_000_000)


def _make_router(monkeypatch: pytest.MonkeyPatch) -> SmartRouter:
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
    store = IdempoStore(ttl_seconds=60)
    monkeypatch.setattr("app.router.smart_router.get_state", lambda: state)
    monkeypatch.setattr("app.router.smart_router.get_market_data", lambda: market)
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    return SmartRouter(idempo_store=store)


@pytest.fixture
def _patch_time(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], _TimeController]:
    def factory(start: float) -> _TimeController:
        controller = _TimeController(start)
        monkeypatch.setattr("app.router.smart_router.time.time", controller.time)
        monkeypatch.setattr("app.router.smart_router.time.time_ns", controller.time_ns)
        monkeypatch.setattr("app.router.timeouts.time", controller.time)
        return controller

    return factory


def test_ack_timeout_expires_pending_order(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _patch_time: Callable[[float], _TimeController],
) -> None:
    monkeypatch.setenv("FF_ORDER_TIMEOUTS", "1")
    monkeypatch.setenv("SUBMIT_ACK_TIMEOUT_SEC", "1")
    monkeypatch.setenv("FILL_TIMEOUT_SEC", "30")
    controller = _patch_time(1000.0)
    router = _make_router(monkeypatch)

    caplog.set_level(logging.WARNING)
    submission = router.register_order(
        strategy="alpha",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        ts_ns=int(controller.time_ns()),
        nonce=1,
    )
    order_id = submission["client_order_id"]
    assert submission["state"] == OrderState.PENDING

    controller.advance(2.0)
    router._run_order_timeouts(now=controller.time())

    snapshot = router.get_order_snapshot(order_id)
    assert snapshot["state"] == OrderState.EXPIRED
    assert any(
        "order-timeout" in record.message
        and order_id in record.message
        and "ack-timeout" in record.message
        for record in caplog.records
    )


def test_fill_timeout_expires_after_partial(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _patch_time: Callable[[float], _TimeController],
) -> None:
    monkeypatch.setenv("FF_ORDER_TIMEOUTS", "1")
    monkeypatch.setenv("SUBMIT_ACK_TIMEOUT_SEC", "5")
    monkeypatch.setenv("FILL_TIMEOUT_SEC", "5")
    controller = _patch_time(2000.0)
    router = _make_router(monkeypatch)

    caplog.set_level(logging.WARNING)
    submission = router.register_order(
        strategy="beta",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=2.0,
        ts_ns=int(controller.time_ns()),
        nonce=2,
    )
    order_id = submission["client_order_id"]
    assert submission["state"] == OrderState.PENDING

    assert router.process_order_event(client_order_id=order_id, event="ack") == OrderState.ACK
    assert (
        router.process_order_event(
            client_order_id=order_id,
            event="partial_fill",
            quantity=1.0,
        )
        == OrderState.PARTIAL
    )

    controller.advance(6.0)
    router._run_order_timeouts(now=controller.time())

    snapshot = router.get_order_snapshot(order_id)
    assert snapshot["state"] == OrderState.EXPIRED
    assert any(
        "order-timeout" in record.message
        and order_id in record.message
        and "fill-timeout" in record.message
        for record in caplog.records
    )


def test_timeouts_disabled_no_expire(
    monkeypatch: pytest.MonkeyPatch,
    _patch_time: Callable[[float], _TimeController],
) -> None:
    monkeypatch.setenv("FF_ORDER_TIMEOUTS", "0")
    controller = _patch_time(3000.0)
    router = _make_router(monkeypatch)

    submission = router.register_order(
        strategy="gamma",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        ts_ns=int(controller.time_ns()),
        nonce=3,
    )
    order_id = submission["client_order_id"]
    assert submission["state"] == OrderState.PENDING

    controller.advance(10.0)
    router._run_order_timeouts(now=controller.time())

    snapshot = router.get_order_snapshot(order_id)
    assert snapshot["state"] == OrderState.PENDING
