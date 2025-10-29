from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import pytest
from starlette.requests import Request

from app.services import runtime
from app.services import operator_dashboard


class _DummyWatchdog:
    def __init__(self, state: Mapping[str, Mapping[str, Any]]):
        self._state = state

    def get_state(self) -> Mapping[str, Mapping[str, Any]]:
        return self._state

    def is_critical(self, name: str) -> bool:
        return bool(self._state.get(name))


@pytest.mark.asyncio
async def test_exchange_watchdog_alert_triggers_notifications(monkeypatch) -> None:
    runtime.reset_for_tests()

    watchdog_state = {
        "binance": {"reachable": False, "rate_limited": False, "error": "unreachable"}
    }
    dummy_watchdog = _DummyWatchdog(watchdog_state)
    monkeypatch.setattr(runtime, "get_exchange_watchdog", lambda: dummy_watchdog)

    alerts: list[dict[str, Any]] = []

    def _send_watchdog_alert(
        exchange: str,
        reason: str,
        mode: str = "AUTO_HOLD",
        *,
        timestamp: str | None = None,
        hold_reason: str | None = None,
        context: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        ts_value = timestamp or "2024-01-01T00:00:00+00:00"
        record = {
            "ts": ts_value,
            "kind": "watchdog_alert",
            "text": (
                f"[ALERT] Exchange watchdog triggered {mode}\n"
                f"Exchange: {exchange}\n"
                f"Reason: {reason}\n"
                "Status: HOLD active; trading paused"
            ),
            "extra": {
                "exchange": exchange,
                "reason": reason,
                "mode": mode,
                "status": "hold_active",
                "status_text": "HOLD active; trading paused",
                "hold_active": True,
                "timestamp": ts_value,
                "hold_reason": hold_reason,
                "context": context,
            },
        }
        alerts.append(record)
        return record

    monkeypatch.setattr("app.opsbot.notifier.send_watchdog_alert", _send_watchdog_alert)
    monkeypatch.setattr(
        "app.opsbot.notifier.get_recent_alerts",
        lambda *, limit=100, since=None: list(alerts),
    )

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
    assert details.get("hold_active") is True
    assert "timestamp" in details

    assert alerts, "expected watchdog alert to be sent"
    alert = alerts[0]
    assert alert["kind"] == "watchdog_alert"
    assert "Exchange watchdog triggered" in alert["text"]
    assert "AUTO_HOLD" in alert["text"]
    assert "binance" in alert["text"].lower()
    assert alert["extra"].get("status") == "hold_active"
    assert alert["extra"].get("exchange") == "binance"

    @dataclass
    class _EdgeContext:
        status: str = "ok"

    @dataclass
    class _Limits:
        value: int = 0

    class _Control:
        mode = "HOLD"
        safe_mode = False
        dry_run = False
        dry_run_mode = False
        flags: dict[str, Any] = {}
        two_man_rule = True

    class _SafetyCounters:
        def as_dict(self) -> dict[str, Any]:
            return {}

    class _SafetyLimits:
        def as_dict(self) -> dict[str, Any]:
            return {}

    class _Safety:
        limits = _SafetyLimits()
        counters = _SafetyCounters()
        hold_reason = "exchange_watchdog:binance unreachable"
        hold_active = True

        def as_dict(self) -> dict[str, Any]:
            return {
                "hold_reason": self.hold_reason,
                "hold_active": self.hold_active,
                "hold_since": "2024-01-01T00:00:00+00:00",
                "last_released_ts": None,
            }

    class _Risk:
        limits = _Limits()

    class _Autopilot:
        def as_dict(self) -> dict[str, Any]:
            return {}

    class _State:
        control = _Control()
        risk = _Risk()
        autopilot = _Autopilot()
        safety = _Safety()

    class _AutoHedgeState:
        enabled = False
        last_execution_result = ""

        def as_dict(self) -> dict[str, Any]:
            return {}

    class _StrategyRiskManager:
        def full_snapshot(self) -> dict[str, Any]:
            return {}

    async def _async_positions_snapshot(*_: Any, **__: Any) -> dict[str, Any]:
        return {"positions": [], "exposure": {}, "totals": {}}

    async def _async_risk_snapshot(*_: Any, **__: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(operator_dashboard, "get_state", lambda: _State())
    monkeypatch.setattr(operator_dashboard, "load_runtime_payload", lambda: {})
    monkeypatch.setattr(operator_dashboard, "get_auto_hedge_state", lambda: _AutoHedgeState())
    monkeypatch.setattr(operator_dashboard, "list_positions", lambda: [])
    monkeypatch.setattr(operator_dashboard, "build_positions_snapshot", _async_positions_snapshot)
    monkeypatch.setattr(operator_dashboard, "build_pnl_snapshot", lambda *a, **k: {})
    monkeypatch.setattr(operator_dashboard, "build_risk_snapshot", _async_risk_snapshot)
    monkeypatch.setattr(
        operator_dashboard,
        "get_strategy_risk_manager",
        lambda: _StrategyRiskManager(),
    )
    monkeypatch.setattr(operator_dashboard.risk_alerts, "evaluate_alerts", lambda: [])
    monkeypatch.setattr(operator_dashboard, "list_pending_requests", lambda **_: [])
    monkeypatch.setattr(operator_dashboard, "list_recent_operator_actions", lambda **_: [])
    monkeypatch.setattr(operator_dashboard, "list_recent_events", lambda **_: [])
    monkeypatch.setattr(operator_dashboard, "list_recent_snapshots", lambda **_: [])
    monkeypatch.setattr(operator_dashboard, "list_recent_execution_stats", lambda **_: [])
    monkeypatch.setattr(operator_dashboard, "get_liquidity_status", lambda: {})
    monkeypatch.setattr(operator_dashboard, "get_reconciliation_status", lambda: {})
    monkeypatch.setattr(operator_dashboard, "edge_guard_allowed", lambda: (True, ""))
    monkeypatch.setattr(
        operator_dashboard,
        "edge_guard_current_context",
        lambda: _EdgeContext(),
    )
    monkeypatch.setattr(
        operator_dashboard.adaptive_risk_advisor,
        "generate_risk_advice",
        lambda *_, **__: {},
    )
    monkeypatch.setattr(operator_dashboard.strategy_orchestrator, "compute_next_plan", lambda: {})
    monkeypatch.setattr(operator_dashboard, "load_latest_report", lambda: None)
    monkeypatch.setattr(operator_dashboard, "get_last_opportunity_state", lambda: ({}, "idle"))

    class _AppState:
        auto_hedge_daemon = type("_Daemon", (), {"_task": None})()
        opportunity_scanner = type("_Scanner", (), {"_task": None})()

    class _App:
        state = _AppState()

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 0),
        "server": ("testserver", 80),
        "app": _App(),
    }
    request = Request(scope)
    context = await operator_dashboard.build_dashboard_context(request)

    last_alert = context.get("last_watchdog_alert")
    assert last_alert is not None
    assert last_alert.get("exchange") == "binance"
    assert "unreachable" in last_alert.get("reason", "")
    assert last_alert.get("timestamp") == alerts[0]["extra"]["timestamp"]
