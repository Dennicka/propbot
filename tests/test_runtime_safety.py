import os

import pytest

from app.services import runtime
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
    assert runtime.is_hold_active() is False

    runtime.engage_safety_hold("unit_test_hold", source="pytest")

    assert runtime.is_hold_active() is True
    safety = runtime.get_safety_status()
    assert safety["hold_active"] is True
    assert safety["hold_reason"] == "unit_test_hold"
    assert safety["limits"]["max_orders_per_min"] == 300
    assert safety["limits"]["max_cancels_per_min"] == 600


def test_resume_request_and_confirm_flow(monkeypatch):
    runtime.engage_safety_hold("unit_test_hold", source="pytest")

    request = runtime.record_resume_request("ready_to_resume", requested_by="alice")
    assert request["reason"] == "ready_to_resume"
    assert request["requested_by"] == "alice"
    assert request["pending"] is True

    result = runtime.approve_resume(actor="bob")
    assert result["hold_cleared"] is True
    safety = result["safety"]
    assert safety["hold_active"] is False
    resume_info = safety["resume_request"]
    assert resume_info["pending"] is False
    assert resume_info["approved_by"] == "bob"


def test_order_counter_triggers_hold(monkeypatch):
    monkeypatch.setenv("MAX_ORDERS_PER_MIN", "1")
    runtime.reset_for_tests()
    register_order_attempt(reason="test_counter", source="pytest")
    with pytest.raises(HoldActiveError):
        register_order_attempt(reason="test_counter", source="pytest")
    assert is_hold_active() is True


def test_cancel_counter_triggers_hold(monkeypatch):
    monkeypatch.setenv("MAX_CANCELS_PER_MIN", "1")
    runtime.reset_for_tests()
    register_cancel_attempt(reason="test_counter", source="pytest")
    with pytest.raises(HoldActiveError):
        register_cancel_attempt(reason="test_counter", source="pytest")
    assert is_hold_active() is True
