from __future__ import annotations

import csv

import pytest

pytest_plugins = ["tests.test_ops_report_endpoint"]


@pytest.mark.parametrize("exclude_sim", [True, False])
def test_ops_report_csv_flat_budget_rows(client, ops_report_environment, monkeypatch, exclude_sim):
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "6")
    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", str(exclude_sim).lower())

    def dynamic_pnl_attribution():
        return {
            "generated_at": "2024-03-02T00:00:00+00:00",
            "by_strategy": {
                "alpha": {
                    "realized": 40.0,
                    "unrealized": 2.0,
                    "fees": 0.3,
                    "rebates": 0.05,
                    "funding": 0.1,
                    "net": 41.85,
                }
            },
            "by_venue": {
                "binance": {
                    "realized": 38.0,
                    "unrealized": 1.5,
                    "fees": 0.25,
                    "rebates": 0.03,
                    "funding": 0.1,
                    "net": 39.38,
                }
            },
            "totals": {
                "realized": 40.0,
                "unrealized": 2.0,
                "fees": 0.3,
                "rebates": 0.05,
                "funding": 0.1,
                "net": 41.85,
            },
            "meta": {"exclude_simulated": exclude_sim},
            "simulated_excluded": exclude_sim,
        }

    monkeypatch.setattr("app.services.ops_report.build_pnl_attribution", dynamic_pnl_attribution)
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
        "attrib_scope",
        "attrib_name",
        "attrib_realized",
        "attrib_unrealized",
        "attrib_fees",
        "attrib_rebates",
        "attrib_funding",
        "attrib_net",
        "attrib_simulated_excluded",
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

    totals_row = next((row for row in rows if row.get("attrib_scope") == "totals"), None)
    assert totals_row is not None

    json_response = client.get(
        "/api/ui/ops_report",
        headers=ops_report_environment["viewer"],
    )
    assert json_response.status_code == 200
    json_payload = json_response.json()
    attrib_totals = json_payload["pnl_attribution"]["totals"]

    assert float(totals_row["attrib_realized"]) == pytest.approx(attrib_totals["realized"])
    assert float(totals_row["attrib_unrealized"]) == pytest.approx(attrib_totals["unrealized"])
    assert float(totals_row["attrib_fees"]) == pytest.approx(attrib_totals["fees"])
    assert float(totals_row["attrib_rebates"]) == pytest.approx(attrib_totals["rebates"])
    assert float(totals_row["attrib_funding"]) == pytest.approx(attrib_totals["funding"])
    assert float(totals_row["attrib_net"]) == pytest.approx(attrib_totals["net"])
    assert json_payload["pnl_attribution"]["simulated_excluded"] is exclude_sim
    assert totals_row["attrib_simulated_excluded"] == str(exclude_sim)
