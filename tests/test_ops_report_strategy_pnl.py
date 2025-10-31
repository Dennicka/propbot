from __future__ import annotations

import csv
from datetime import datetime, timezone

from app.strategy.pnl_tracker import get_strategy_pnl_tracker


def test_ops_report_includes_strategy_pnl_section(client, monkeypatch):
    tracker = get_strategy_pnl_tracker()
    now_ts = datetime.now(timezone.utc).timestamp()
    tracker.record_fill("alpha", 12.5, ts=now_ts)
    tracker.record_fill("beta", -7.5, ts=now_ts, simulated=True)

    response = client.get("/api/ui/ops_report")
    assert response.status_code == 200
    payload = response.json()
    pnl_block = payload.get("strategy_pnl")
    assert pnl_block is not None
    assert pnl_block["simulated_excluded"] is True
    names = [entry["name"] for entry in pnl_block["strategies"]]
    assert names == ["alpha"]

    csv_response = client.get("/api/ui/ops_report.csv")
    assert csv_response.status_code == 200
    rows = list(csv.DictReader(csv_response.text.splitlines()))
    assert any(
        row["section"] == "strategy_pnl" and row["key"] == "simulated_excluded"
        for row in rows
    )
    assert any(
        row["section"] == "strategy_pnl:alpha" and row["key"] == "realized_today"
        for row in rows
    )
    assert not any(row["section"] == "strategy_pnl:beta" for row in rows)

    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", "false")
    response_with_simulated = client.get("/api/ui/ops_report")
    assert response_with_simulated.status_code == 200
    payload_with_sim = response_with_simulated.json()
    names_with_sim = [entry["name"] for entry in payload_with_sim["strategy_pnl"]["strategies"]]
    assert "beta" in names_with_sim
