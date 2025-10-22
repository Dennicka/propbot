from __future__ import annotations

import pytest

from app import ledger
from app.broker.router import ExecutionRouter
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
    stub = _StubClient()
    runtime = state.derivatives
    assert runtime is not None
    assert "binance_um" in runtime.venues
    runtime.venues["binance_um"].client = stub

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
    assert any(call[0] == "place" for call in stub.calls)

    await router.cancel_order(venue="binance-um", order_id=order["order_id"])
    cancelled = ledger.get_order(order["order_id"])
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"

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
    assert any(call[0] == "cancel" for call in stub.calls)

    open_orders = state.open_orders
    assert isinstance(open_orders, list)
    assert any(entry.get("id") == replacement["order_id"] for entry in open_orders)
