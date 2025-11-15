from __future__ import annotations

import pytest

from app.services import runtime as runtime_module


def test_live_promotion_endpoint_smoke(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMOTION_STAGE", "testnet_sandbox")
    monkeypatch.setenv("EXEC_PROFILE", "testnet")
    monkeypatch.delenv("PROMOTION_ALLOWED_NEXT", raising=False)
    monkeypatch.setattr(runtime_module, "_PROFILE", None, raising=False)

    response = client.get("/api/ui/live-promotion")
    assert response.status_code == 200

    payload = response.json()
    assert payload["stage"] == "testnet_sandbox"
    assert payload["runtime_profile"] == "testnet"
    assert payload["is_live_profile"] is False
    assert isinstance(payload["allowed_next_stages"], list)
    assert "live_dry_run" in payload["allowed_next_stages"]
