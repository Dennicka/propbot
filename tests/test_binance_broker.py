from __future__ import annotations

import pytest

from app import ledger
from datetime import datetime, timezone

from app.broker.binance import BinanceLiveBroker, BinanceTestnetBroker, _Credentials
from app.broker.paper import PaperBroker
from app.broker.router import ExecutionRouter
from app.services.reconciler import FillReconciler
from app.services.runtime import get_state, reset_for_tests


@pytest.mark.asyncio
async def test_binance_broker_respects_safe_mode(monkeypatch):
    ledger.reset()
    reset_for_tests()
    broker = BinanceTestnetBroker(safe_mode=True, dry_run=False, credentials=_Credentials("k", "s"))

    calls: list[tuple[tuple, dict]] = []

    async def fake_request(*args, **kwargs):  # pragma: no cover - defensive stub
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(broker, "_request", fake_request)

    result = await broker.create_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.1,
        price=20_000.0,
    )

    assert result["order_id"] > 0
    order = ledger.get_order(result["order_id"])
    assert order is not None
    assert order["status"] == "skipped"
    assert calls == []


def test_broker_selection_depends_on_profile(monkeypatch):
    ledger.reset()
    reset_for_tests()
    state = get_state()
    state.control.environment = "paper"
    router_paper = ExecutionRouter()
    brokers_paper = router_paper.brokers()
    assert isinstance(brokers_paper["binance-um"], PaperBroker)

    state.control.environment = "testnet"
    router_testnet = ExecutionRouter()
    brokers_testnet = router_testnet.brokers()
    assert isinstance(brokers_testnet["binance-um"], BinanceTestnetBroker)

    state.control.environment = "live"
    router_live = ExecutionRouter()
    brokers_live = router_live.brokers()
    assert isinstance(brokers_live["binance-um"], BinanceLiveBroker)


@pytest.mark.asyncio
async def test_binance_broker_fetches_recently_closed_symbols(monkeypatch):
    ledger.reset()
    reset_for_tests()
    ts_open = datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat()
    order_open = ledger.record_order(
        venue="binance-um",
        symbol="ETHUSDT",
        side="buy",
        qty=0.5,
        price=2000.0,
        status="filled",
        client_ts=ts_open,
        exchange_ts=ts_open,
        idemp_key="test-open",
    )
    ledger.record_fill(
        order_id=order_open,
        venue="binance-um",
        symbol="ETHUSDT",
        side="buy",
        qty=0.5,
        price=2000.0,
        fee=1.0,
        ts=ts_open,
    )

    broker = BinanceTestnetBroker(
        safe_mode=False, dry_run=False, credentials=_Credentials("k", "s")
    )

    async def fake_active_symbols():
        return []

    monkeypatch.setattr(broker, "_active_symbols", fake_active_symbols)

    requested: list[str] = []

    async def fake_request(method, path, *, params=None, signed=False):
        if path == "/fapi/v1/userTrades":
            requested.append(params["symbol"])
            return [
                {
                    "symbol": "ETHUSDT",
                    "qty": "0.5",
                    "price": "2100.0",
                    "commission": "0.9",
                    "buyer": False,
                    "time": 1_700_000_000_000,
                }
            ]
        if path == "/fapi/v2/account":
            return {"assets": [], "positions": []}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(broker, "_request", fake_request)

    fills = await broker.get_fills()

    assert requested == ["ETHUSDT"], "broker should request fills for the closed symbol"
    assert any(fill["symbol"] == "ETHUSDT" for fill in fills)


@pytest.mark.asyncio
async def test_fill_reconciler_accounts_for_closed_positions(monkeypatch):
    ledger.reset()
    reset_for_tests()
    ts_open = datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat()
    order_open = ledger.record_order(
        venue="binance-um",
        symbol="ETHUSDT",
        side="buy",
        qty=0.5,
        price=2000.0,
        status="filled",
        client_ts=ts_open,
        exchange_ts=ts_open,
        idemp_key="test-open",
    )
    ledger.record_fill(
        order_id=order_open,
        venue="binance-um",
        symbol="ETHUSDT",
        side="buy",
        qty=0.5,
        price=2000.0,
        fee=1.0,
        ts=ts_open,
    )

    broker = BinanceTestnetBroker(
        safe_mode=False, dry_run=False, credentials=_Credentials("k", "s")
    )

    async def fake_active_symbols():
        return []

    async def fake_positions():
        return []

    async def fake_request(method, path, *, params=None, signed=False):
        if path == "/fapi/v1/userTrades":
            return [
                {
                    "symbol": "ETHUSDT",
                    "qty": "0.5",
                    "price": "2100.0",
                    "commission": "0.9",
                    "buyer": False,
                    "time": 1_700_000_000_000,
                }
            ]
        if path == "/fapi/v2/account":
            return {"assets": [], "positions": []}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(broker, "_active_symbols", fake_active_symbols)
    monkeypatch.setattr(broker, "get_positions", fake_positions)
    monkeypatch.setattr(broker, "_request", fake_request)

    class _Router:
        def brokers(self):
            return {"binance-um": broker}

    reconciler = FillReconciler(router=_Router())
    result = await reconciler.run_once()

    assert any(fill["symbol"] == "ETHUSDT" for fill in result["fills"])
    pnl = ledger.compute_pnl()
    assert pnl["realized"] == pytest.approx(48.1, rel=1e-6)
    assert pnl["unrealized"] == pytest.approx(0.0, abs=1e-9)
