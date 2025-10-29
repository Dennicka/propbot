from __future__ import annotations

from app.version import APP_VERSION


def test_audit_snapshot_endpoint(client, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "audit-snapshot")

    unauthorized = client.get("/api/ui/audit_snapshot")
    assert unauthorized.status_code in {401, 403}

    headers = {"Authorization": "Bearer audit-snapshot"}
    response = client.get("/api/ui/audit_snapshot", headers=headers)
    assert response.status_code == 200
    payload = response.json()

    assert payload["build_version"] == APP_VERSION
    assert "hold_active" in payload
    assert "control" in payload
    assert "safety" in payload
    assert "autopilot" in payload
    assert "positions" in payload
    assert "open_positions" in payload
    assert "partial_positions" in payload
    assert "exposure" in payload
    assert "totals" in payload
    assert "strategy_risk" in payload
    assert "universe" in payload

    universe_snapshot = payload["universe"]
    assert "candidates" in universe_snapshot
    assert "top_pairs" in universe_snapshot
    assert "allowed_symbols" in universe_snapshot

    strategy_snapshot = payload["strategy_risk"]
    assert isinstance(strategy_snapshot, dict)
    assert "strategies" in strategy_snapshot

    positions_payload = payload["positions"]
    assert isinstance(positions_payload, dict)
    assert "positions" in positions_payload
    assert "exposure" in positions_payload
    assert "totals" in positions_payload


