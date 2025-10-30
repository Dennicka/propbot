from __future__ import annotations

from typing import Any

import pytest

from app.exchange_watchdog import get_exchange_watchdog, reset_exchange_watchdog_for_tests
from app.services import runtime
from app.services.exchange_watchdog_runner import ExchangeWatchdogRunner


@pytest.mark.asyncio
async def test_watchdog_auto_hold_engages_hold_and_audit(monkeypatch) -> None:
    runtime.reset_for_tests()
    reset_exchange_watchdog_for_tests()

    monkeypatch.setenv("WATCHDOG_ENABLED", "true")
    monkeypatch.setenv("WATCHDOG_AUTO_HOLD", "true")
    monkeypatch.delenv("NOTIFY_WATCHDOG", raising=False)

    watchdog = get_exchange_watchdog()
    runner = ExchangeWatchdogRunner(watchdog, interval=0.01)

    def _probe() -> dict[str, object]:
        return {"binance": {"ok": False, "reason": "ping failure"}}

    runner.set_probe(_probe)

    audit_events: list[dict[str, Any]] = []

    def _log(operator: str, role: str, action: str, details: dict[str, Any] | None = None) -> None:
        audit_events.append(
            {
                "operator": operator,
                "role": role,
                "action": action,
                "details": dict(details or {}),
            }
        )

    monkeypatch.setattr(runtime, "log_operator_action", _log)

    await runner.check_once()

    state = runtime.get_state()
    assert state.safety.hold_active is True
    assert state.safety.hold_reason == "exchange_watchdog:binance ping failure"

    actions = [event["action"] for event in audit_events]
    assert "AUTO_HOLD_WATCHDOG" in actions
    recorded = next(event for event in audit_events if event["action"] == "AUTO_HOLD_WATCHDOG")
    assert recorded["details"].get("exchange") == "binance"
    assert recorded["details"].get("reason") == "ping failure"


@pytest.mark.asyncio
async def test_watchdog_notifications_on_status_changes(monkeypatch) -> None:
    runtime.reset_for_tests()
    reset_exchange_watchdog_for_tests()

    monkeypatch.setenv("WATCHDOG_ENABLED", "true")
    monkeypatch.setenv("NOTIFY_WATCHDOG", "true")
    monkeypatch.delenv("WATCHDOG_AUTO_HOLD", raising=False)

    watchdog = get_exchange_watchdog()
    runner = ExchangeWatchdogRunner(watchdog, interval=0.01)

    statuses = [
        {"binance": {"ok": True, "reason": ""}},
        {"binance": {"ok": False, "reason": "timeout"}},
        {"binance": {"ok": True, "reason": ""}},
    ]
    index = {"value": 0}

    def _probe() -> dict[str, object]:
        payload = statuses[index["value"]]
        if index["value"] < len(statuses) - 1:
            index["value"] += 1
        return payload

    runner.set_probe(_probe)

    emitted: list[dict[str, Any]] = []

    def _emit(kind: str, text: str, *, extra: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        record = {"kind": kind, "text": text, "extra": dict(extra or {})}
        emitted.append(record)
        return record

    monkeypatch.setattr("app.services.exchange_watchdog_runner.notifier.emit_alert", _emit)

    await runner.check_once()  # initial ok
    await runner.check_once()  # ok -> fail
    await runner.check_once()  # fail -> ok

    assert len(emitted) == 2
    down_event, up_event = emitted
    assert down_event["kind"] == "watchdog_status"
    assert down_event["extra"].get("current") is False
    assert down_event["extra"].get("exchange") == "binance"
    assert up_event["extra"].get("current") is True
    assert up_event["extra"].get("exchange") == "binance"
