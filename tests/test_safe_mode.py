from types import SimpleNamespace
from unittest.mock import MagicMock

from app.router.smart_router import SmartRouter
from app.services.safe_mode import (
    SafeMode,
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
    SafeMode.set(False)


def teardown_function(function) -> None:
    reset_safe_mode_for_tests()
    SafeMode.set(False)


def _make_router(monkeypatch) -> SmartRouter:
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    monkeypatch.setattr("app.router.smart_router.get_profile", lambda: SimpleNamespace(name="test"))
    monkeypatch.setattr("app.router.smart_router.is_live", lambda profile: False)
    monkeypatch.setattr("app.router.smart_router.ff.md_watchdog_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.risk_limits_on", lambda: False)
    state = SimpleNamespace(config=SimpleNamespace(data=None))
    return SmartRouter(state=state, market_data={})


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


def test_safe_mode_blocks_router_intent(monkeypatch) -> None:
    SafeMode.set(True)
    router = _make_router(monkeypatch)

    result = router.register_order(
        strategy="test",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=None,
        ts_ns=1,
        nonce=1,
    )

    assert result == {"ok": False, "reason": "safe-mode", "cost": None}


def test_safe_mode_disabled_allows_router_flow(monkeypatch) -> None:
    SafeMode.set(False)
    router = _make_router(monkeypatch)
    router._idempo.should_send = MagicMock(return_value=False)

    result = router.register_order(
        strategy="test",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=None,
        ts_ns=2,
        nonce=1,
    )

    assert result.get("status") == "idempotent_skip"
