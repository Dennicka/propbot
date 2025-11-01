from __future__ import annotations

import pytest

from app.services import runtime
from app.watchdog.exchange_watchdog import (
    get_exchange_watchdog,
    reset_exchange_watchdog_for_tests,
)


@pytest.mark.acceptance
def test_smoke_health_metrics_watchdog(client):
    runtime.reset_for_tests()
    app = client.app
    app.state.resume_ok = True
    app.state.opportunity_scanner = None
    app.state.auto_hedge_daemon = None

    reset_exchange_watchdog_for_tests()
    watchdog = get_exchange_watchdog()
    watchdog.check_once(
        lambda: {
            "binance": {"ok": True},
            "okx": {"ok": True},
        }
    )
    assert watchdog.overall_ok() is True

    health = client.get("/healthz")
    assert health.status_code == 200, health.text
    payload = health.json()
    assert payload["ok"] is True
    assert payload["journal_ok"] is True
    assert payload["config_ok"] is True

    readiness = client.get("/live-readiness")
    assert readiness.status_code == 200
    readiness_payload = readiness.json()
    assert readiness_payload["ready"] is True
    assert readiness_payload["leader"] is True

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    body = metrics.text
    for metric in (
        "api_latency_seconds",
        "market_data_staleness_seconds",
        "order_errors_total",
        "watchdog_state",
    ):
        assert metric in body

    badges = client.get("/api/ui/runtime_badges")
    assert badges.status_code == 200
    badges_payload = badges.json()
    assert badges_payload["watchdog"] == "OK"
    assert badges_payload["risk_checks"] in {"ON", "AUTO", "OFF"}
    assert badges_payload["daily_loss"] in {"OK", "WARN", "OFF"}
