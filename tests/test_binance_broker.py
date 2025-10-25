from __future__ import annotations

import pytest

from app import ledger
from app.broker.binance import BinanceTestnetBroker, _Credentials
from app.broker.paper import PaperBroker
from app.broker.router import ExecutionRouter
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
    assert isinstance(brokers_live["binance-um"], BinanceTestnetBroker)
