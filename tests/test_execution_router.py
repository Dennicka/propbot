from __future__ import annotations

import pytest

from app import ledger
from app.broker.binance import BinanceTestnetBroker
from app.broker.router import ExecutionRouter
from app.services.arbitrage import Plan, PlanLeg
from app.services.runtime import get_state, reset_for_tests


class _StubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def place_order(self, **kwargs):
        self.calls.append(("place", dict(kwargs)))
        return {"ok": True}

    def cancel_order(self, **kwargs):
        self.calls.append(("cancel", dict(kwargs)))
        return {"ok": True}

    def positions(self):  # pragma: no cover - compatibility shim
        return []


@pytest.mark.asyncio
async def test_execution_router_places_and_replaces_orders(monkeypatch):
    monkeypatch.setenv("ENABLE_PLACE_TEST_ORDERS", "1")
    monkeypatch.setenv("BINANCE_UM_API_KEY_TESTNET", "stub")
    monkeypatch.setenv("BINANCE_UM_API_SECRET_TESTNET", "stub")
    monkeypatch.setenv("OKX_API_KEY_TESTNET", "stub")
    monkeypatch.setenv("OKX_API_SECRET_TESTNET", "stub")
    monkeypatch.setenv("OKX_API_PASSPHRASE_TESTNET", "stub")
    reset_for_tests()
    ledger.reset()
    state = get_state()
    state.control.safe_mode = False
    state.control.dry_run = False
    state.control.environment = "testnet"
    runtime = state.derivatives
    assert runtime is not None
    assert "binance_um" in runtime.venues

    async def fake_request(self, method, path, *, params=None, signed=False):
        self._last_request = {"method": method, "path": path, "params": dict(params or {}), "signed": signed}
        return {"status": "NEW", "orderId": 123456789}

    monkeypatch.setattr(BinanceTestnetBroker, "_request", fake_request)

    router = ExecutionRouter()
    order = await router.place_limit_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.01,
        price=25_000.0,
        client_order_id="router-test",
    )
    assert order["venue"] == "binance-um"
    stored = ledger.get_order(order["order_id"])
    assert stored is not None
    assert stored["status"] == "open"
    broker = router.broker_for_venue("binance-um")
    assert isinstance(broker, BinanceTestnetBroker)
    assert getattr(broker, "_last_request", {}).get("path") == "/fapi/v1/order"
    assert getattr(broker, "_last_request", {}).get("method") == "POST"

    await router.cancel_order(venue="binance-um", order_id=order["order_id"])
    cancelled = ledger.get_order(order["order_id"])
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert getattr(broker, "_last_request", {}).get("method") == "DELETE"

    replacement = await router.replace_limit_order(
        venue="binance-um",
        order_id=order["order_id"],
        price=24_900.0,
        symbol="BTCUSDT",
        side="buy",
        qty=0.02,
        client_order_id="router-test-r",
    )
    replaced_record = ledger.get_order(replacement["order_id"])
    assert replaced_record is not None
    assert replaced_record["status"] == "open"
    assert replaced_record["qty"] == pytest.approx(0.02)
    assert getattr(broker, "_last_request", {}).get("path") == "/fapi/v1/order"
    assert getattr(broker, "_last_request", {}).get("method") == "POST"

    open_orders = state.open_orders
    assert isinstance(open_orders, list)
    assert any(entry.get("id") == replacement["order_id"] for entry in open_orders)


@pytest.mark.asyncio
async def test_execute_plan_blocked_by_risk(monkeypatch):
    reset_for_tests()
    ledger.reset()
    state = get_state()
    state.control.safe_mode = False
    state.control.dry_run = False
    state.risk.limits.max_open_orders = {"__default__": 0}
    plan = Plan(
        symbol="BTCUSDT",
        notional=100.0,
        used_slippage_bps=0,
        used_fees_bps={"binance": 0, "okx": 0},
        viable=True,
    )
    plan.legs = [
        PlanLeg(exchange="binance", side="buy", price=20_000.0, qty=0.005, fee_usdt=0.0),
        PlanLeg(exchange="okx", side="sell", price=20_010.0, qty=0.005, fee_usdt=0.0),
    ]
    router = ExecutionRouter()
    with pytest.raises(PermissionError):
        await router.execute_plan(plan)
