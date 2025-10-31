import pytest

from app.services import runtime
from app.services.autopilot_guard import AutopilotGuard


class DummyWatchdog:
    def __init__(self, snapshots):
        self._snapshots = list(snapshots)
        self._index = 0

    def get_state(self):
        if not self._snapshots:
            return {}
        idx = min(self._index, len(self._snapshots) - 1)
        self._index += 1
        return self._snapshots[idx]


@pytest.mark.asyncio
async def test_autopilot_guard_watchdog(monkeypatch):
    runtime.reset_for_tests()
    state = runtime.get_state()
    state.control.auto_loop = True

    holds: list[str] = []

    async def fake_hold():
        holds.append("called")
        state.control.auto_loop = False
        return state.loop

    monkeypatch.setattr("app.services.autopilot_guard.hold_loop", fake_hold)

    audit_events = []

    def fake_log(operator: str, role: str, action: str, details):
        audit_events.append((operator, role, action, details))

    monkeypatch.setattr("app.services.autopilot_guard.log_operator_action", fake_log)

    watchdog = DummyWatchdog(
        [
            {"binance": {"status": "OK", "reason": ""}},
            {"binance": {"status": "AUTO_HOLD", "reason": "latency"}},
        ]
    )

    guard = AutopilotGuard(interval=0.1, watchdog=watchdog)

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
    assert details["reason"] == "WATCHDOG_AUTO_HOLD"
    assert details["exchange"] == "binance"
