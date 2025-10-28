import pytest

from app.services import runtime
from app.services.autopilot import evaluate_startup
from app.services.runtime import get_autopilot_state, get_state, is_hold_active, update_guard


@pytest.fixture(autouse=True)
def reset_runtime_after_test():
    yield
    runtime.reset_for_tests()


@pytest.mark.asyncio
async def test_autopilot_resumes_on_clean_start(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_ENABLE", "true")
    runtime.reset_for_tests()
    runtime.set_preflight_result(True)
    state = get_state()
    state.autopilot.target_mode = "RUN"
    state.autopilot.target_safe_mode = False
    state.safety.hold_reason = state.safety.hold_reason or "restart_safe_mode"

    await evaluate_startup()

    control = get_state().control
    assert control.mode == "RUN"
    assert control.safe_mode is False
    assert is_hold_active() is False
    autopilot_state = get_autopilot_state()
    assert autopilot_state.last_action == "resume"
    assert autopilot_state.armed is True


@pytest.mark.asyncio
async def test_autopilot_refuses_when_guard_blocks(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_ENABLE", "true")
    runtime.reset_for_tests()
    runtime.set_preflight_result(True)
    state = get_state()
    state.autopilot.target_mode = "RUN"
    state.autopilot.target_safe_mode = False
    update_guard("runaway_breaker", "HOLD", "pytest")

    await evaluate_startup()

    assert is_hold_active() is True
    autopilot_state = get_autopilot_state()
    assert autopilot_state.last_action == "refused"
    assert "runaway_guard" in str(autopilot_state.last_reason)
    assert autopilot_state.armed is False


@pytest.mark.asyncio
async def test_autopilot_disabled_keeps_manual_resume(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_ENABLE", "false")
    runtime.reset_for_tests()
    runtime.set_preflight_result(True)
    state = get_state()
    state.autopilot.target_mode = "RUN"
    state.autopilot.target_safe_mode = False

    await evaluate_startup()

    assert is_hold_active() is True
    autopilot_state = get_autopilot_state()
    assert autopilot_state.last_action in {"disabled", "none"}
    assert autopilot_state.armed is False
