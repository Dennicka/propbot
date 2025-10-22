import pytest

from app import ledger
from app.services import loop
from app.services.runtime import get_loop_state, get_state, reset_for_tests


@pytest.mark.asyncio
async def test_run_cycle_updates_loop_state():
    reset_for_tests()
    ledger.reset()
    result = await loop.run_cycle()
    loop_state = get_loop_state()
    assert result.plan is not None
    assert loop_state.last_plan is not None
    assert loop_state.last_summary is not None
    assert loop_state.cycles_completed == 1
    # loop_cycle event should be persisted even if plan not viable
    events = ledger.fetch_events(5)
    assert events


@pytest.mark.asyncio
async def test_resume_and_hold_toggle_auto_loop():
    reset_for_tests()
    ledger.reset()
    state = get_state()
    state.control.safe_mode = False
    assert state.control.auto_loop is False
    await loop.resume_loop()
    assert state.control.auto_loop is True
    stop_state = await loop.stop_loop()
    assert stop_state.status == "STOPPING"
    assert get_state().control.auto_loop is False
    await loop.hold_loop()
    assert state.control.auto_loop is False


@pytest.mark.asyncio
async def test_cancel_all_orders_clears_open_orders():
    reset_for_tests()
    ledger.reset()
    state = get_state()
    state.control.environment = "testnet"
    order_id = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.1,
        price=25_000.0,
        status="submitted",
        client_ts="2024-01-01T00:00:00Z",
        exchange_ts=None,
        idemp_key="order-1",
    )
    result = await loop.cancel_all_orders()
    assert result == {"cancelled": 1, "failed": 0}
    order = ledger.get_order(order_id)
    assert order is not None
    assert order["status"] == "cancelled"
