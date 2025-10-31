import pytest

from app.services import runtime
from app.services.autopilot_guard import AutopilotGuard


@pytest.mark.asyncio
async def test_autopilot_guard_daily_loss(monkeypatch):
    runtime.reset_for_tests()
    state = runtime.get_state()
    state.control.auto_loop = True

    holds: list[str] = []

    async def fake_hold():
        holds.append("called")
        state.control.auto_loop = False
        return state.loop

    monkeypatch.setattr("app.services.autopilot_guard.hold_loop", fake_hold)

    audit_events: list[tuple[str, str, str, dict]] = []

    def fake_log(operator: str, role: str, action: str, details):
        audit_events.append((operator, role, action, details))

    monkeypatch.setattr("app.services.autopilot_guard.log_operator_action", fake_log)

    snapshots = [
        {"enabled": True, "blocking": True, "breached": False},
        {"enabled": True, "blocking": True, "breached": True, "max_daily_loss_usdt": 100.0},
    ]
    call = {"count": 0}

    def daily_loss_provider():
        index = min(call["count"], len(snapshots) - 1)
        call["count"] += 1
        return snapshots[index]

    guard = AutopilotGuard(interval=0.1, daily_loss_provider=daily_loss_provider, watchdog=None)

    await guard.evaluate_once()
    assert state.control.auto_loop is True

    await guard.evaluate_once()

    assert state.control.auto_loop is False
    assert holds == ["called"]
    assert len(audit_events) == 1
    operator, role, action, details = audit_events[0]
    assert operator == "system"
    assert role == "system"
    assert action == "AUTO_TRADE_OFF"
    assert details["reason"] == "DAILY_LOSS_BREACH"
