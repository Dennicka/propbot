from app.services.safe_mode import (
    SafeModeState,
    enter_hold,
    enter_kill,
    get_safe_mode_state,
    is_closure_allowed,
    is_opening_allowed,
    is_trading_allowed,
    reset_safe_mode_for_tests,
)


def setup_function(function) -> None:
    reset_safe_mode_for_tests()


def teardown_function(function) -> None:
    reset_safe_mode_for_tests()


def test_enter_hold_blocks_openings() -> None:
    assert is_opening_allowed() is True
    status = enter_hold("unit-test")
    assert status.state is SafeModeState.HOLD
    assert is_opening_allowed() is False
    assert is_trading_allowed() is False
    assert is_closure_allowed() is True


def test_enter_kill_is_terminal() -> None:
    enter_kill("panic")
    status = get_safe_mode_state()
    assert status.state is SafeModeState.KILL
    assert is_opening_allowed() is False
    assert is_trading_allowed() is False
    assert is_closure_allowed() is False
    enter_hold("should_not_change")
    assert get_safe_mode_state().state is SafeModeState.KILL
