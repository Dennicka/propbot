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
    await loop.hold_loop()
    assert state.control.auto_loop is False
