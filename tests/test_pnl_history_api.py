import pytest

from pnl_history_store import append_snapshot, reset_store


def test_pnl_history_requires_token(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    response = client.get("/api/ui/pnl_history")
    assert response.status_code in {401, 403}


def test_pnl_history_returns_snapshots(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "super-secret")
    reset_store()
    append_snapshot(
        {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "unrealized_pnl_total": 10.5,
            "pnl_totals": {"unrealized": 10.5},
            "total_exposure_usd": {"binance-um": 1000.0},
            "total_exposure_usd_total": 1000.0,
            "open_positions": 1,
            "partial_positions": 0,
            "open_positions_total": 1,
            "simulated": {"per_venue": {}, "total": 0.0, "positions": 0},
        }
    )

    headers = {"Authorization": "Bearer super-secret"}
    response = client.get("/api/ui/pnl_history", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    snapshots = payload.get("snapshots")
    assert isinstance(snapshots, list)
    assert payload.get("count") == len(snapshots)
    assert snapshots
    assert snapshots[0]["unrealized_pnl_total"] == 10.5
