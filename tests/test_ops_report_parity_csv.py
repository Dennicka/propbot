from __future__ import annotations

import csv

import pytest

pytest_plugins = ["tests.test_ops_report_endpoint"]


def test_ops_report_csv_flat_budget_rows(client, ops_report_environment, monkeypatch):
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "6")
    monkeypatch.setattr(
        "app.services.ops_report.get_risk_accounting_snapshot",
        lambda: {
            "per_strategy": {
                "alpha": {
                    "budget": {
                        "limit_usdt": 900.0,
                        "used_today_usdt": 300.0,
                        "remaining_usdt": 600.0,
                    }
                },
                "gamma": {
                    "budget": {
                        "limit_usdt": 0.0,
                        "used_today_usdt": 0.0,
                        "remaining_usdt": 0.0,
                    }
                },
            }
        },
    )

    response = client.get("/api/ui/ops_report.csv", headers=ops_report_environment["viewer"])
    assert response.status_code == 200
    reader = csv.DictReader(response.text.splitlines())
    rows = list(reader)
    assert rows
    header = reader.fieldnames
    assert header == [
        "timestamp",
        "open_trades_count",
        "max_open_trades_limit",
        "daily_loss_status",
        "watchdog_status",
        "auto_trade",
        "strategy",
        "budget_usdt",
        "used_usdt",
        "remaining_usdt",
    ]
    assert any(row["strategy"] == "alpha" for row in rows)
    assert any(row["strategy"] == "gamma" for row in rows)

    alpha_row = next(row for row in rows if row["strategy"] == "alpha")
    assert alpha_row["timestamp"]
    assert alpha_row["open_trades_count"] == "1"
    assert alpha_row["max_open_trades_limit"] == "6"
    assert alpha_row["daily_loss_status"]
    assert alpha_row["watchdog_status"]
    assert alpha_row["auto_trade"]
    assert float(alpha_row["budget_usdt"]) == pytest.approx(900.0)
    assert float(alpha_row["used_usdt"]) == pytest.approx(300.0)
    assert float(alpha_row["remaining_usdt"]) == pytest.approx(600.0)
