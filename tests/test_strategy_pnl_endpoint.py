from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.strategy.pnl_tracker import get_strategy_pnl_tracker


def test_strategy_pnl_endpoint_aggregation(client):
    tracker = get_strategy_pnl_tracker()
    now = datetime.now(timezone.utc)
    tracker.record_fill("alpha", 100.0, ts=(now - timedelta(hours=1)).timestamp())
    tracker.record_fill("alpha", -50.0, ts=(now - timedelta(days=2)).timestamp())
    tracker.record_fill("beta", -10.0, ts=(now - timedelta(minutes=30)).timestamp())
    tracker.record_fill("beta", 5.0, ts=(now - timedelta(days=9)).timestamp())

    response = client.get("/api/ui/strategy_pnl")
    assert response.status_code == 200
    payload = response.json()

    assert payload["simulated_excluded"] is True
    strategies = payload["strategies"]
    assert [row["name"] for row in strategies] == ["beta", "alpha"]

    alpha = next(row for row in strategies if row["name"] == "alpha")
    assert alpha["realized_today"] == pytest.approx(100.0)
    assert alpha["realized_7d"] == pytest.approx(50.0)
    assert alpha["max_drawdown_7d"] == pytest.approx(50.0)

    beta = next(row for row in strategies if row["name"] == "beta")
    assert beta["realized_today"] == pytest.approx(-10.0)
    assert beta["realized_7d"] == pytest.approx(-10.0)
    assert beta["max_drawdown_7d"] == pytest.approx(10.0)


def test_strategy_pnl_endpoint_simulated_exclusion(client, monkeypatch):
    tracker = get_strategy_pnl_tracker()
    now_ts = datetime.now(timezone.utc).timestamp()
    tracker.record_fill("alpha", 10.0, ts=now_ts)
    tracker.record_fill("simulated", 25.0, ts=now_ts, simulated=True)

    response = client.get("/api/ui/strategy_pnl")
    assert response.status_code == 200
    payload = response.json()
    names = {row["name"] for row in payload["strategies"]}
    assert payload["simulated_excluded"] is True
    assert "alpha" in names
    assert "simulated" not in names

    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", "false")
    response_include = client.get("/api/ui/strategy_pnl")
    assert response_include.status_code == 200
    payload_include = response_include.json()
    names_include = {row["name"] for row in payload_include["strategies"]}
    assert payload_include["simulated_excluded"] is False
    assert "simulated" in names_include
