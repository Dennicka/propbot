from __future__ import annotations


def test_ops_status_snapshot_endpoint(client, monkeypatch):
    monkeypatch.setattr("app.ui.ops_status.is_daily_loss_cap_breached", lambda: False)

    response = client.get("/api/ui/status")
    assert response.status_code == 200

    payload = response.json()

    assert set(
        [
            "runtime_profile",
            "is_live_profile",
            "health_ok",
            "readiness_ok",
            "market_data_ok",
            "live_trading_allowed",
            "pnl_cap_hit",
            "health_reason",
            "readiness_reason",
            "live_trading_reason",
        ]
    ).issubset(payload)

    assert isinstance(payload["runtime_profile"], str)
    assert isinstance(payload["is_live_profile"], bool)
    assert isinstance(payload["health_ok"], bool)
    assert isinstance(payload["readiness_ok"], bool)
    assert "live_trading_allowed" in payload
