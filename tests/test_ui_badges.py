import pytest

from app.services.runtime import get_state
from app.watchdog.exchange_watchdog import (
    get_exchange_watchdog,
    reset_exchange_watchdog_for_tests,
)


def test_runtime_badges_endpoint_reflects_live_state(client, monkeypatch):
    reset_exchange_watchdog_for_tests()
    state = get_state()
    state.control.auto_loop = True
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
    assert payload == {
        "auto_trade": "ON",
        "risk_checks": "ON",
        "daily_loss": "OK",
        "watchdog": "OK",
    }

    # Dry-run mode should force auto trade badge to OFF even if toggled on.
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    state.control.auto_loop = True
    rerun = client.get("/api/ui/runtime_badges")
    assert rerun.status_code == 200
    assert rerun.json()["auto_trade"] == "OFF"


def test_runtime_badges_endpoint_failure_modes(client, monkeypatch):
    reset_exchange_watchdog_for_tests()
    state = get_state()
    state.control.auto_loop = False
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
    watchdog.check_once(
        lambda: {"binance": {"ok": False, "reason": "maintenance"}}
    )

    daily_loss_state["breached"] = True

    response = client.get("/api/ui/runtime_badges")
    assert response.status_code == 200
    payload = response.json()
    assert payload["auto_trade"] == "OFF"
    assert payload["risk_checks"] == "OFF"
    assert payload["daily_loss"] == "BREACH"
    assert payload["watchdog"] == "DEGRADED"

    watchdog.mark_auto_hold("binance", reason="auto-hold")
    follow_up = client.get("/api/ui/runtime_badges")
    assert follow_up.status_code == 200
    assert follow_up.json()["watchdog"] == "AUTO_HOLD"
