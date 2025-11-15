from __future__ import annotations

import pytest

from app.approvals.live_toggle import LiveToggleEffectiveState


def test_live_guard_endpoint_smoke(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    monkeypatch.setenv("LIVE_TRADING_ALLOWED_VENUES", "binance_perp,okx_perp")
    monkeypatch.setenv("LIVE_TRADING_ALLOWED_STRATEGIES", "arb_v1")
    state = LiveToggleEffectiveState(
        enabled=True,
        last_action="enable_live",
        last_status="approved",
        last_updated_at=None,
        last_request_id="req-1",
        requestor_id="ops1",
        approver_id="ops2",
        resolution_reason="ship it",
    )

    class _Store:
        def get_effective_state(self) -> LiveToggleEffectiveState:
            return state

    monkeypatch.setattr("app.runtime.live_guard.get_live_toggle_store", lambda: _Store())
    response = client.get("/api/ui/live-guard")
    assert response.status_code == 200
    payload = response.json()

    assert payload["runtime_profile"]
    assert payload["state"] in {"enabled", "disabled", "test_only"}
    assert isinstance(payload["allowed_venues"], list)
    assert isinstance(payload["allowed_strategies"], list)
    assert "promotion_stage" in payload
    assert "promotion_reason" in payload
    assert "promotion_allowed_next_stages" in payload
    assert payload["approvals_enabled"] is True
    assert payload["approvals_last_request_id"] == "req-1"
    assert payload["approvals_last_action"] == "enable_live"
    assert payload["approvals_last_status"] == "approved"
