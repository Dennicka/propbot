from __future__ import annotations

import pytest

from app.services import runtime
from app.watchdog.exchange_watchdog import get_exchange_watchdog, reset_exchange_watchdog_for_tests


@pytest.mark.acceptance
def test_basic_flow(client, monkeypatch):
    for name in (
        "FEATURE_CHAOS",
        "CHAOS_PROFILE",
        "CHAOS_WS_DROP_P",
        "CHAOS_REST_TIMEOUT_P",
        "CHAOS_ORDER_DELAY_MS",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_exchange_watchdog_for_tests()
    runtime.reset_for_tests()
    app = client.app
    app.state.resume_ok = True
    app.state.opportunity_scanner = None
    app.state.auto_hedge_daemon = None

    health = client.get("/healthz")
    payload = health.json()
    assert health.status_code == 200, payload
    assert payload["ok"] is True
    assert payload["journal_ok"] is True
    assert payload["config_ok"] is True

    readiness = client.get("/live-readiness")
    readiness_payload = readiness.json()
    assert readiness.status_code == 200, readiness_payload
    assert readiness_payload["ready"] is True
    assert readiness_payload["leader"] is True

    monkeypatch.setenv("FEATURE_CHAOS", "1")
    monkeypatch.setenv("CHAOS_PROFILE", "mild")
    runtime.reset_for_tests()
    app.state.resume_ok = True
    app.state.opportunity_scanner = None
    app.state.auto_hedge_daemon = None

    state = runtime.get_state()
    assert state.control.mode == "HOLD"
    assert state.control.safe_mode is True
    assert state.chaos.enabled is True
    assert state.chaos.profile == "mild"

    watchdog = get_exchange_watchdog()
    assert watchdog.overall_ok() is True

    chaos_response = client.get("/api/ui/chaos")
    assert chaos_response.status_code == 200
    chaos_payload = chaos_response.json()
    assert chaos_payload["enabled"] is True
    assert chaos_payload["profile"] == "mild"
    assert chaos_payload["ws_drop_p"] == pytest.approx(0.05, rel=1e-9)
    assert chaos_payload["rest_timeout_p"] == pytest.approx(0.02, rel=1e-9)
    assert chaos_payload["order_delay_ms"] == 150
