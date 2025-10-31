import pytest

from app.services.ops_report import build_ops_report
from app.services.runtime import get_state
from app.watchdog.exchange_watchdog import (
    get_exchange_watchdog,
    reset_exchange_watchdog_for_tests,
)


@pytest.mark.asyncio
async def test_ops_report_includes_badges(monkeypatch):
    reset_exchange_watchdog_for_tests()
    state = get_state()
    state.control.auto_loop = False
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "false")

    daily_loss_state = {"breached": True}

    def fake_daily_loss():
        return dict(daily_loss_state)

    monkeypatch.setattr(
        "app.services.runtime_badges.get_daily_loss_cap_state",
        fake_daily_loss,
    )

    watchdog = get_exchange_watchdog()
    watchdog.check_once(
        lambda: {"okx": {"ok": False, "reason": "outage"}}
    )

    report = await build_ops_report()
    assert "badges" in report
    badges = report["badges"]
    assert badges == {
        "auto_trade": "OFF",
        "risk_checks": "OFF",
        "daily_loss": "BREACH",
        "watchdog": "DEGRADED",
        "partial_hedges": "OK",
    }
