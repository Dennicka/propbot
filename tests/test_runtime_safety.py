import json

import json

import pytest

from app.services import runtime, approvals_store
from app.services.runtime import (
    HoldActiveError,
    is_hold_active,
    register_cancel_attempt,
    register_order_attempt,
)


@pytest.fixture(autouse=True)
def reset_runtime_state(monkeypatch):
    monkeypatch.delenv("MAX_ORDERS_PER_MIN", raising=False)
    monkeypatch.delenv("MAX_CANCELS_PER_MIN", raising=False)
    runtime.reset_for_tests()
    yield
    runtime.reset_for_tests()


def test_engage_safety_hold_sets_flag_and_status():
    if runtime.is_hold_active():
        runtime.record_resume_request("release_for_test", requested_by="pytest")
        runtime.approve_resume(actor="pytest")
    assert runtime.is_hold_active() is False

    runtime.engage_safety_hold("unit_test_hold", source="pytest")

    assert runtime.is_hold_active() is True
    safety = runtime.get_safety_status()
    assert safety["hold_active"] is True
    assert safety["hold_reason"] == "unit_test_hold"
    assert safety["limits"]["max_orders_per_min"] == 300
    assert safety["limits"]["max_cancels_per_min"] == 600


def test_restart_bootstraps_hold_and_safe_mode():
    safety = runtime.get_safety_status()
    assert safety["hold_active"] is True
    control = runtime.get_state().control
    assert control.mode == "HOLD"
    assert control.safe_mode is True


def test_resume_request_and_confirm_flow(monkeypatch):
    runtime.engage_safety_hold("unit_test_hold", source="pytest")

    request = runtime.record_resume_request("ready_to_resume", requested_by="alice")
    assert request["id"]
    assert request["reason"] == "ready_to_resume"
    assert request["requested_by"] == "alice"
    assert request["pending"] is True
    pending = approvals_store.get_request(request["id"])
    assert pending is not None
    assert pending["status"] == "pending"

    result = runtime.approve_resume(request_id=request["id"], actor="bob")
    assert result["hold_cleared"] is True
    safety = result["safety"]
    assert safety["hold_active"] is False
    resume_info = safety["resume_request"]
    assert resume_info["pending"] is False
    assert resume_info["approved_by"] == "bob"
    approved = approvals_store.get_request(request["id"])
    assert approved is not None
    assert approved["status"] == "approved"


def test_risk_limit_raise_request_and_approval():
    record = runtime.request_risk_limit_change(
        "max_position_usdt",
        "BTCUSDT",
        250.0,
        reason="increase coverage",
        requested_by="pytest",
    )
    assert record["status"] == "pending"
    result = runtime.approve_risk_limit_change(record["id"], actor="reviewer")
    limits = runtime.get_state().risk.limits.max_position_usdt
    assert pytest.approx(limits["BTCUSDT"]) == 250.0
    approved = approvals_store.get_request(record["id"])
    assert approved is not None
    assert approved["status"] == "approved"
    assert result["result"]["limit"] == "max_position_usdt"


def test_exit_dry_run_flow():
    control = runtime.get_state().control
    control.dry_run = True
    control.dry_run_mode = True
    record = runtime.request_exit_dry_run("go_live", requested_by="pytest")
    assert record["status"] == "pending"
    runtime.approve_exit_dry_run(record["id"], actor="reviewer")
    control_state = runtime.get_state().control
    assert control_state.dry_run is False
    assert control_state.dry_run_mode is False
    approved = approvals_store.get_request(record["id"])
    assert approved is not None
    assert approved["status"] == "approved"


def test_order_counter_triggers_hold(monkeypatch):
    monkeypatch.setenv("MAX_ORDERS_PER_MIN", "1")
    runtime.reset_for_tests()
    runtime.record_resume_request("counter_test_orders", requested_by="pytest")
    runtime.approve_resume(actor="pytest")
    register_order_attempt(reason="test_counter", source="pytest")
    with pytest.raises(HoldActiveError):
        register_order_attempt(reason="test_counter", source="pytest")
    assert is_hold_active() is True


def test_cancel_counter_triggers_hold(monkeypatch):
    monkeypatch.setenv("MAX_CANCELS_PER_MIN", "1")
    runtime.reset_for_tests()
    runtime.record_resume_request("counter_test_cancels", requested_by="pytest")
    runtime.approve_resume(actor="pytest")
    register_cancel_attempt(reason="test_counter", source="pytest")
    with pytest.raises(HoldActiveError):
        register_cancel_attempt(reason="test_counter", source="pytest")
    assert is_hold_active() is True


def test_watchdog_blocks_order_attempt(monkeypatch):
    runtime.reset_for_tests()
    runtime.record_resume_request("watchdog", requested_by="pytest")
    runtime.approve_resume(actor="pytest")

    audit_events: list[dict[str, object]] = []

    def _capture_log(operator: str, role: str, action: str, details=None) -> None:
        audit_events.append(
            {
                "operator": operator,
                "role": role,
                "action": action,
                "details": dict(details or {}),
            }
        )

    monkeypatch.setattr(runtime, "log_operator_action", _capture_log)
    monkeypatch.setattr(runtime, "send_notifier_alert", lambda *_, **__: None)

    class _StubWatchdog:
        def __init__(self) -> None:
            self._entry = {"ok": False, "last_check_ts": 0.0, "reason": "rate_limited"}

        def get_state(self) -> dict[str, dict[str, object]]:
            return {"binance": dict(self._entry)}

        def overall_ok(self) -> bool:
            return False

        def most_recent_failure(self):
            return "binance", dict(self._entry)

    monkeypatch.setattr(runtime, "get_exchange_watchdog", lambda: _StubWatchdog())

    with pytest.raises(HoldActiveError) as excinfo:
        register_order_attempt(reason="watchdog", source="unit-test")

    assert "exchange_watchdog" in excinfo.value.reason
    assert runtime.is_hold_active() is True
    safety = runtime.get_safety_status()
    assert safety["hold_active"] is True
    assert safety["hold_reason"].startswith("exchange_watchdog:")
    assert "rate_limited" in safety["hold_reason"]

    recorded_actions = [entry["action"] for entry in audit_events]
    assert "AUTO_HOLD_WATCHDOG" in recorded_actions


def test_hold_reason_persisted_to_runtime_store(monkeypatch, tmp_path):
    runtime_path = tmp_path / "runtime_state.json"
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(runtime_path))
    runtime.reset_for_tests()

    runtime.engage_safety_hold("audit_hold", source="pytest")

    payload = json.loads(runtime_path.read_text())
    assert payload["safety"]["hold_active"] is True
    assert payload["safety"]["hold_reason"] == "audit_hold"
