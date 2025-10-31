from fastapi.testclient import TestClient

from app.metrics import slo
from app import server_ws


def test_metrics_endpoint_exposes_expected_metrics():
    slo.reset_for_tests()
    with TestClient(server_ws.app) as ws_client:
        response = ws_client.get("/metrics")
    assert response.status_code == 200
    body = response.text
    for metric in (
        "propbot_order_cycle_ms",
        "propbot_ws_gap_ms",
        "propbot_skipped_by_reason_total",
        "propbot_watchdog_ok",
        "propbot_daily_loss_breached",
        "propbot_trades_executed_total",
        "propbot_risk_breaches_total",
        "propbot_auto_trade",
        "propbot_watchdog_state",
        "propbot_daily_loss_breach",
    ):
        assert metric in body
