from datetime import datetime, timedelta, timezone
from pathlib import Path


def test_partial_hedge_alert_activation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPS_ALERTS_FILE", str(tmp_path / "ops_alerts.json"))
    monkeypatch.setenv("PARTIAL_HEDGE_ALERT_SECONDS", "1")

    from app.services import risk_alerts

    risk_alerts.reset_for_tests()

    now = datetime.now(timezone.utc)
    stale_ts = (now - timedelta(minutes=10)).isoformat()

    class _DummyCounters:
        cancels_last_min = 0

    class _DummyLimits:
        max_cancels_per_min = 100

    class _DummySafety:
        hold_active = False
        hold_reason = ""
        hold_since = None
        hold_source = None
        limits = _DummyLimits()
        counters = _DummyCounters()

    class _DummyAuto:
        enabled = False

    class _DummyState:
        safety = _DummySafety()
        auto_hedge = _DummyAuto()

    monkeypatch.setattr(risk_alerts, "get_state", lambda: _DummyState())
    monkeypatch.setattr(
        risk_alerts,
        "list_position_records",
        lambda: [
            {
                "id": "abc123",
                "symbol": "BTCUSDT",
                "status": "partial",
                "timestamp": stale_ts,
                "notional_usdt": 5000.0,
                "legs": [
                    {"status": "filled"},
                    {"status": "open"},
                ],
            }
        ],
    )

    active = risk_alerts.evaluate_alerts(now=now)
    assert any(alert["kind"] == "partial_hedge_stalled" for alert in active)

    audit_entries = risk_alerts.notifier.read_audit_events()
    kinds = [entry.get("kind") for entry in audit_entries]
    assert "partial_hedge_stalled" in kinds

    risk_alerts.reset_for_tests()
