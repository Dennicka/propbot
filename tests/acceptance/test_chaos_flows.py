from __future__ import annotations

import os

import pytest

from app.services import runtime
from app.watchdog.exchange_watchdog import (
    get_exchange_watchdog,
    reset_exchange_watchdog_for_tests,
)

_CHAOS_ENABLED = os.getenv("CHAOS_ENABLED", "false").lower() in {"1", "true", "yes"}


@pytest.mark.acceptance
@pytest.mark.skipif(not _CHAOS_ENABLED, reason="chaos acceptance disabled")
def test_chaos_flow(client, monkeypatch):
    monkeypatch.setenv("FEATURE_CHAOS", "1")
    monkeypatch.setenv("CHAOS_PROFILE", "aggressive")
    monkeypatch.setenv("CHAOS_WS_DROP_P", "0.15")
    monkeypatch.setenv("CHAOS_REST_TIMEOUT_P", "0.05")
    monkeypatch.setenv("CHAOS_ORDER_DELAY_MS", "250")

    runtime.reset_for_tests()
    app = client.app
    app.state.resume_ok = True
    app.state.opportunity_scanner = None
    app.state.auto_hedge_daemon = None

    state = runtime.get_state()
    assert state.chaos.enabled is True
    assert state.chaos.profile == "aggressive"

    reset_exchange_watchdog_for_tests()
    watchdog = get_exchange_watchdog()
    watchdog.check_once(
        lambda: {
            "binance": {"ok": True},
            "okx": {"ok": True},
        }
    )
    assert watchdog.overall_ok() is True

    chaos_response = client.get("/api/ui/chaos")
    assert chaos_response.status_code == 200
    payload = chaos_response.json()
    assert payload["enabled"] is True
    assert payload["profile"] == "aggressive"
    assert payload["ws_drop_p"] == pytest.approx(0.15, rel=1e-9)
    assert payload["rest_timeout_p"] == pytest.approx(0.05, rel=1e-9)
    assert payload["order_delay_ms"] == 250
