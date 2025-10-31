from __future__ import annotations

import csv
from datetime import datetime, timezone

import pytest

from app.strategy.pnl_tracker import get_strategy_pnl_tracker


def test_ops_report_includes_strategy_pnl_section(client, monkeypatch):
    tracker = get_strategy_pnl_tracker()
    now_ts = datetime.now(timezone.utc).timestamp()
    tracker.record_fill("alpha", 12.5, ts=now_ts)
    tracker.record_fill("beta", -7.5, ts=now_ts, simulated=True)
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "4")
    monkeypatch.setattr(
        "app.services.ops_report.get_risk_accounting_snapshot",
        lambda: {
            "per_strategy": {
                "alpha": {
                    "budget": {
                        "limit_usdt": 500.0,
                        "used_today_usdt": 120.0,
                        "remaining_usdt": 380.0,
                    }
                }
            }
        },
    )

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
    assert rows
    alpha_row = next(row for row in rows if row["strategy"] == "alpha")
    assert float(alpha_row["budget_usdt"]) == pytest.approx(500.0)
    assert float(alpha_row["used_usdt"]) == pytest.approx(120.0)
    assert float(alpha_row["remaining_usdt"]) == pytest.approx(380.0)
    assert alpha_row["daily_loss_status"]
    assert alpha_row["watchdog_status"]
    assert alpha_row["auto_trade"]

    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", "false")
    response_with_simulated = client.get("/api/ui/ops_report")
    assert response_with_simulated.status_code == 200
    payload_with_sim = response_with_simulated.json()
    names_with_sim = [entry["name"] for entry in payload_with_sim["strategy_pnl"]["strategies"]]
    assert "beta" in names_with_sim
