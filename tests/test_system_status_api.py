import json

from app.version import APP_VERSION
from datetime import datetime, timedelta, timezone

from app.services import runtime


def test_status_overview_contract(client):
    resp = client.get("/api/ui/status/overview")
    assert resp.status_code == 200
    payload = resp.json()

    for field in ("ts", "overall", "scores", "slo", "components", "alerts"):
        assert field in payload

    assert payload["build_version"] == APP_VERSION

    assert "hold_active" in payload
    assert "resume_request" in payload
    assert "clock_skew_s" in payload
    assert "auto_hedge" in payload
    assert "dry_run_mode" in payload
    auto_block = payload["auto_hedge"]
    for field in (
        "auto_enabled",
        "last_opportunity_checked_ts",
        "last_execution_result",
        "consecutive_failures",
        "on_hold",
    ):
        assert field in auto_block
    safety_block = payload.get("safety")
    assert isinstance(safety_block, dict)
    assert safety_block.get("counters") is not None
    assert safety_block.get("limits") is not None

    assert payload["overall"] in {"OK", "WARN", "ERROR", "HOLD"}

    scores = payload["scores"]
    assert set(scores.keys()) == {"P0", "P1", "P2", "P3"}
    slo = payload["slo"]
    for metric in (
        "ws_gap_ms_p95",
        "order_cycle_ms_p95",
        "reject_rate",
        "cancel_fail_rate",
        "recon_mismatch",
        "max_day_drawdown_bps",
        "budget_remaining",
    ):
        assert metric in slo

    components = payload["components"]
    assert isinstance(components, list)
    assert components, "components list must not be empty"

    required_ids = {
        "journal_outbox",
        "guarded_startup",
        "leader_fencing",
        "conformance",
        "recon",
        "keys_security",
        "compliance_worm",
        "slo_watchdog",
    }
    assert required_ids.issubset({comp["id"] for comp in components})

    for comp in components:
        for field in ("id", "title", "group", "status", "summary", "metrics", "links"):
            assert field in comp
        assert comp["group"] in {"P0", "P1", "P2", "P3"}
        assert comp["status"] in {"OK", "WARN", "ERROR", "HOLD"}
        assert isinstance(comp["metrics"], dict)
        assert isinstance(comp["links"], list)

    alerts = payload["alerts"]
    assert isinstance(alerts, list)
    for alert in alerts:
        for field in ("severity", "title", "msg", "since", "component_id"):
            assert field in alert


def test_status_stream_websocket_smoke(client):
    with client.websocket_connect("/api/ui/status/stream/status") as ws:
        message = ws.receive_text()
        payload = json.loads(message)
        assert "overall" in payload
        assert "components" in payload
        assert payload.get("build_version") == APP_VERSION


def test_critical_slo_triggers_auto_hold(client):
    runtime.reset_for_tests()
    state = runtime.get_state()
    state.control.mode = "RUN"
    state.control.safe_mode = False
    state.control.auto_loop = True
    state.loop.running = True
    state.loop.status = "RUN"
    state.metrics.slo["ws_gap_ms_p95"] = 5_000.0
    breach_started = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    state.metrics.slo_breach_started_at["ws_gap_ms_p95"] = breach_started

    resp = client.get("/api/ui/status/overview")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["overall"] == "HOLD"
    assert state.control.mode == "HOLD"
    assert state.control.safe_mode is True
    assert state.control.auto_loop is False
    assert state.loop.status == "HOLD"
    assert state.loop.running is False

    runtime.reset_for_tests()
