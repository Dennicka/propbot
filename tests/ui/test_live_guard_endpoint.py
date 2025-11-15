from __future__ import annotations

import pytest


def test_live_guard_endpoint_smoke(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    monkeypatch.setenv("LIVE_TRADING_ALLOWED_VENUES", "binance_perp,okx_perp")
    monkeypatch.setenv("LIVE_TRADING_ALLOWED_STRATEGIES", "arb_v1")
    response = client.get("/api/ui/live-guard")
    assert response.status_code == 200
    payload = response.json()

    assert payload["runtime_profile"]
    assert payload["state"] in {"enabled", "disabled", "test_only"}
    assert isinstance(payload["allowed_venues"], list)
    assert isinstance(payload["allowed_strategies"], list)
