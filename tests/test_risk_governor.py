from types import SimpleNamespace

import pytest

from app import risk_governor
from app.exchange_watchdog import get_exchange_watchdog, reset_exchange_watchdog_for_tests
from app.services import portfolio, risk, runtime


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch):
    monkeypatch.setenv("CLOCK_SKEW_HOLD_THRESHOLD_MS", "1000")
    monkeypatch.delenv("MAX_DAILY_LOSS_USD", raising=False)
    monkeypatch.delenv("MAX_TOTAL_NOTIONAL_USD", raising=False)
    monkeypatch.delenv("MAX_TOTAL_NOTIONAL_USDT", raising=False)
    monkeypatch.delenv("MAX_UNREALIZED_LOSS_USD", raising=False)
    runtime.reset_for_tests()
    reset_exchange_watchdog_for_tests()
    yield
    runtime.reset_for_tests()
    reset_exchange_watchdog_for_tests()


async def _stub_snapshot(**kwargs):
    position = portfolio.PortfolioPosition(
        venue="binance-um",
        venue_type="perp",
        symbol="BTCUSDT",
        qty=0.0,
        notional=0.0,
        entry_px=0.0,
        mark_px=0.0,
        upnl=0.0,
        rpnl=0.0,
    )
    return portfolio.PortfolioSnapshot(
        positions=[position],
        balances=[],
        pnl_totals=kwargs.get("pnl_totals", {"realized": 0.0, "unrealized": 0.0, "total": 0.0}),
        notional_total=kwargs.get("notional_total", 0.0),
    )


@pytest.mark.asyncio
async def test_risk_limit_breach_sets_hold(monkeypatch):
    monkeypatch.setenv("MAX_DAILY_LOSS_USD", "5")

    async def fake_snapshot():
        return await _stub_snapshot(
            pnl_totals={"realized": -10.0, "unrealized": 0.0, "total": -10.0}
        )

    monkeypatch.setattr(portfolio, "snapshot", fake_snapshot)
    monkeypatch.setattr(risk_governor, "_collect_clock_skew_ms", lambda state: None)
    monkeypatch.setattr(risk_governor, "_check_maintenance", lambda state: (False, []))

    def fake_refresh_runtime_state(*, snapshot=None, open_orders=None):
        return SimpleNamespace(current=SimpleNamespace(daily_loss_usdt=-10.0))

    monkeypatch.setattr(risk, "refresh_runtime_state", fake_refresh_runtime_state)

    reason = await risk_governor.validate(context="test")
    assert reason == "risk_limit breach: MAX_DAILY_LOSS_USD"

    safety = runtime.get_safety_status()
    assert safety["hold_active"] is True
    assert safety["hold_reason"] == reason
    assert safety.get("risk_snapshot")


@pytest.mark.asyncio
async def test_clock_skew_triggers_hold(monkeypatch):
    monkeypatch.setenv("CLOCK_SKEW_HOLD_THRESHOLD_MS", "150")

    async def fake_snapshot():
        return await _stub_snapshot()

    monkeypatch.setattr(portfolio, "snapshot", fake_snapshot)
    monkeypatch.setattr(risk_governor, "_check_maintenance", lambda state: (False, []))
    monkeypatch.setattr(risk_governor, "_collect_clock_skew_ms", lambda state: 250.0)

    def fake_refresh_runtime_state(*, snapshot=None, open_orders=None):
        return SimpleNamespace(current=SimpleNamespace(daily_loss_usdt=0.0))

    monkeypatch.setattr(risk, "refresh_runtime_state", fake_refresh_runtime_state)

    reason = await risk_governor.validate(context="test")
    assert reason == "clock_skew"

    safety = runtime.get_safety_status()
    assert safety["hold_reason"] == "clock_skew"
    assert safety.get("risk_snapshot", {}).get("clock_skew_ms") == 250.0


@pytest.mark.asyncio
async def test_exchange_watchdog_triggers_auto_hold(monkeypatch):
    watchdog = get_exchange_watchdog()

    def _probe() -> dict[str, object]:
        return {"binance": {"ok": False, "reason": "connection lost"}}

    watchdog.check_once(_probe)

    captured: list[dict[str, object]] = []

    def fake_log_operator_action(name, role, action, details=None):
        captured.append(
            {
                "name": name,
                "role": role,
                "action": action,
                "details": details,
            }
        )

    monkeypatch.setattr(runtime, "log_operator_action", fake_log_operator_action)

    async def fake_snapshot():
        return await _stub_snapshot()

    monkeypatch.setattr(portfolio, "snapshot", fake_snapshot)
    monkeypatch.setattr(risk_governor, "_check_maintenance", lambda state: (False, []))
    monkeypatch.setattr(risk_governor, "_collect_clock_skew_ms", lambda state: None)

    def fake_refresh_runtime_state(*, snapshot=None, open_orders=None):
        return SimpleNamespace(current=SimpleNamespace(daily_loss_usdt=0.0))

    monkeypatch.setattr(risk, "refresh_runtime_state", fake_refresh_runtime_state)

    reason = await risk_governor.validate(context="order_execution")

    assert reason.startswith("exchange_watchdog:")
    assert "binance" in reason
    assert runtime.is_hold_active()

    safety = runtime.get_safety_status()
    assert safety["hold_active"] is True
    assert safety["hold_reason"] == reason

    assert captured, "audit log should record AUTO_HOLD event"
    event = captured[-1]
    assert event["action"] == "AUTO_HOLD_WATCHDOG"
    details = event.get("details") or {}
    assert isinstance(details, dict)
    assert details.get("exchange") == "binance"
    assert details.get("reason") == "connection lost"
