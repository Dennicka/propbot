import pytest

from app.chaos import injector
from app.services import runtime
from app.watchdog.exchange_watchdog import reset_exchange_watchdog_for_tests


@pytest.fixture(autouse=True)
def _reset_state():
    runtime.reset_for_tests()
    runtime.autopilot_apply_resume(safe_mode=False)
    reset_exchange_watchdog_for_tests()
    yield
    runtime.reset_for_tests()
    reset_exchange_watchdog_for_tests()


def test_inject_ws_disconnect_marks_watchdog_degraded():
    result = injector.inject("ws_disconnect", {"venue": "OKX"})

    watchdog = result["watchdog"]
    assert watchdog is not None
    assert watchdog.get("status") == "DEGRADED"

    transition = result["watchdog_transition"]
    assert transition is not None
    assert transition["current"] == "DEGRADED"
    assert transition["previous"] in {"UNKNOWN", "OK"}

    safety = result["safety"]
    assert safety["hold_active"] is False
    assert result["control"]["mode"] == "RUN"


def test_inject_order_reject_engages_hold():
    result = injector.inject("order_reject", {"venue": "OKX"})

    safety = result["safety"]
    assert safety["hold_active"] is True
    assert result["control"]["mode"] == "HOLD"

    details = result.get("details")
    assert details and details["hold_engaged"] is True


def test_inject_latency_spike_marks_reconciliation():
    result = injector.inject("latency_spike_ms", {"venue": "OKX", "ms": 750})

    recon = result["reconciliation"]
    assert recon["desync_detected"] is True
    assert recon["issue_count"] >= 1
    issue = recon["issues"][-1]
    assert issue["kind"] == "latency_spike_ms"
    assert issue["latency_ms"] == 750
    assert issue["venue"] == "OKX"

    safety = result["safety"]
    assert safety["hold_active"] is False


def test_inject_rejects_unknown_kind():
    with pytest.raises(ValueError):
        injector.inject("invalid", {"venue": "OKX"})


def test_inject_requires_venue_for_watchdog_fault():
    with pytest.raises(ValueError):
        injector.inject("rest_429", {})


def test_latency_spike_requires_positive_ms():
    with pytest.raises(ValueError):
        injector.inject("latency_spike_ms", {"venue": "OKX", "ms": 0})
