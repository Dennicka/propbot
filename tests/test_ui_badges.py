import re

import pytest

from app.services.runtime import get_state
from app.watchdog.exchange_watchdog import (
    get_exchange_watchdog,
    reset_exchange_watchdog_for_tests,
)


def test_non_report_includes_badges(client, monkeypatch):
    reset_exchange_watchdog_for_tests()
    state = get_state()
    state.control.auto_loop = True
    state.execution.stuck_resolver.enabled = False
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "true")
    monkeypatch.setenv("DRY_RUN_MODE", "false")

    daily_loss_state = {"breached": False}

    def fake_daily_loss():
        return dict(daily_loss_state)

    monkeypatch.setattr(
        "app.services.runtime_badges.get_daily_loss_cap_state",
        fake_daily_loss,
    )

    watchdog = get_exchange_watchdog()
    watchdog.check_once(lambda: {"binance": {"ok": True}})

    response = client.get("/api/ui/runtime_badges")
    assert response.status_code == 200
    payload = response.json()
    expected = {
        "auto_trade": "ON",
        "risk_checks": "ON",
        "daily_loss": "OK",
        "watchdog": "OK",
        "partial_hedges": "OK",
    }

    assert set(payload.keys()) == set(expected.keys())
    for key, matcher in expected.items():
        value = payload[key]
        assert value == matcher

    assert "stuck_resolver" not in payload

    state.execution.stuck_resolver.enabled = True
    rerun_enabled = client.get("/api/ui/runtime_badges")
    assert rerun_enabled.status_code == 200
    enabled_payload = rerun_enabled.json()
    assert "stuck_resolver" in enabled_payload
    assert re.match(r"^ON \(retries 1h: \d+\)$", enabled_payload["stuck_resolver"])

    # Dry-run mode should force auto trade badge to OFF even if toggled on.
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    state.control.auto_loop = True
    rerun = client.get("/api/ui/runtime_badges")
    assert rerun.status_code == 200
    assert rerun.json()["auto_trade"] == "OFF"


def test_ui_badges_include_stuck_resolver_status(client, monkeypatch):
    reset_exchange_watchdog_for_tests()
    state = get_state()
    state.execution.stuck_resolver.enabled = True
    state.control.auto_loop = False
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "true")
    monkeypatch.setenv("DRY_RUN_MODE", "false")

    monkeypatch.setattr(
        "app.services.runtime_badges.get_daily_loss_cap_state",
        lambda: {"breached": False},
    )

    watchdog = get_exchange_watchdog()
    watchdog.check_once(lambda: {"binance": {"ok": True}})

    response = client.get("/api/ui/runtime_badges")
    assert response.status_code == 200
    payload = response.json()
    assert "stuck_resolver" in payload
    assert re.match(r"^ON \(retries 1h: \d+\)$", payload["stuck_resolver"])


def test_runtime_badges_endpoint_failure_modes(client, monkeypatch):
    reset_exchange_watchdog_for_tests()
    state = get_state()
    state.control.auto_loop = False
    state.execution.stuck_resolver.enabled = False
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "false")

    daily_loss_state = {"breached": False}

    def fake_daily_loss():
        return dict(daily_loss_state)

    monkeypatch.setattr(
        "app.services.runtime_badges.get_daily_loss_cap_state",
        fake_daily_loss,
    )

    watchdog = get_exchange_watchdog()
    watchdog.check_once(lambda: {"binance": {"ok": False, "reason": "maintenance"}})

    daily_loss_state["breached"] = True

    response = client.get("/api/ui/runtime_badges")
    assert response.status_code == 200
    payload = response.json()
    assert payload["auto_trade"] == "OFF"
    assert payload["risk_checks"] == "OFF"
    assert payload["daily_loss"] == "BREACH"
    assert payload["watchdog"] == "DEGRADED"
    assert payload["partial_hedges"] == "OK"
    assert "stuck_resolver" not in payload

    watchdog.mark_auto_hold("binance", reason="auto-hold")
    follow_up = client.get("/api/ui/runtime_badges")
    assert follow_up.status_code == 200
    assert follow_up.json()["watchdog"] == "AUTO_HOLD"


def test_runtime_badges_handles_snapshot_errors(client):
    state = get_state()
    original_resolver = state.execution.stuck_resolver

    class BrokenResolver:
        enabled = True

        def snapshot(self):  # pragma: no cover - invoked during test
            raise RuntimeError("boom")

    try:
        state.execution.stuck_resolver = BrokenResolver()
        response = client.get("/api/ui/runtime_badges")
        assert response.status_code == 200
        payload = response.json()
        assert payload["stuck_resolver"].startswith("ON (")
    finally:
        state.execution.stuck_resolver = original_resolver
