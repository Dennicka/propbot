from __future__ import annotations

import pytest


def test_live_safety_endpoint_snapshot(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    response = client.get("/api/ui/live-safety")

    assert response.status_code == 200

    payload = response.json()
    assert set(
        [
            "runtime_profile",
            "is_live_profile",
            "live_trading_guard_state",
            "live_trading_allowed",
            "promotion_stage",
            "promotion_reason",
            "promotion_allowed_next_stages",
            "live_approvals_enabled",
            "live_approvals_last_status",
        ]
    ).issubset(payload.keys())

    assert isinstance(payload["runtime_profile"], str)
    assert isinstance(payload["is_live_profile"], bool)
    assert isinstance(payload["live_trading_allowed"], bool)
    assert isinstance(payload["live_trading_guard_state"], str)
    assert "live_approvals_enabled" in payload
    assert "live_approvals_last_status" in payload
