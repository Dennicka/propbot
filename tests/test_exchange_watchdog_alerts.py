from __future__ import annotations

from typing import Any, Mapping

from app.services import runtime


class _DummyWatchdog:
    def __init__(self, state: Mapping[str, Mapping[str, Any]]):
        self._state = state

    def get_state(self) -> Mapping[str, Mapping[str, Any]]:
        return self._state

    def is_critical(self, name: str) -> bool:
        return bool(self._state.get(name))


def test_exchange_watchdog_alert_records_audit(monkeypatch) -> None:
    runtime.reset_for_tests()

    watchdog_state = {
        "binance": {"reachable": False, "rate_limited": False, "error": "unreachable"}
    }
    dummy_watchdog = _DummyWatchdog(watchdog_state)
    monkeypatch.setattr(runtime, "get_exchange_watchdog", lambda: dummy_watchdog)

    sent_alerts: list[dict[str, Any]] = []

    def _send(kind: str, text: str, extra: Mapping[str, Any] | None = None) -> None:
        sent_alerts.append({"kind": kind, "text": text, "extra": dict(extra or {})})

    monkeypatch.setattr(runtime, "send_notifier_alert", _send)

    audit_events: list[dict[str, Any]] = []

    def _log(operator: str, role: str, action: str, details: Mapping[str, Any] | None = None) -> None:
        audit_events.append(
            {
                "operator": operator,
                "role": role,
                "action": action,
                "details": dict(details or {}),
            }
        )

    monkeypatch.setattr(runtime, "log_operator_action", _log)

    reason = runtime.evaluate_exchange_watchdog(context="unit-test")

    assert reason is not None
    watchdog_logs = [event for event in audit_events if event["action"] == "WATCHDOG_ALERT"]
    assert watchdog_logs, "expected WATCHDOG_ALERT entry in audit log"
    details = watchdog_logs[0]["details"]
    assert details.get("exchange") == "binance"
    assert details.get("initiated_by") == "system"
    assert "timestamp" in details

    assert sent_alerts, "expected notifier alert to be sent"
    alert = sent_alerts[0]
    assert alert["kind"] == "watchdog_alert"
    assert "Exchange watchdog triggered auto-HOLD" in alert["text"]
    assert "binance" in alert["text"].lower()
    assert alert["extra"].get("status") == "hold_active"
    assert alert["extra"].get("exchange") == "binance"
