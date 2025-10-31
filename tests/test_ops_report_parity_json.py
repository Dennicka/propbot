from __future__ import annotations

import pytest

pytest_plugins = ["tests.test_ops_report_endpoint"]


def test_ops_report_json_includes_parity_fields(client, ops_report_environment, monkeypatch):
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "8")
    actions = [
        {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "operator": "alice",
            "role": "operator",
            "action": "HOLD",
            "details": {"status": "ok"},
        },
        {
            "timestamp": "2024-01-01T00:05:00+00:00",
            "operator_name": "bob",
            "role": "viewer",
            "action": "VIEW",
            "details": "snapshot",
        },
    ]
    monkeypatch.setattr(
        "app.services.ops_report.list_recent_operator_actions",
        lambda limit=10: actions,
    )
    monkeypatch.setattr(
        "app.services.ops_report.get_risk_accounting_snapshot",
        lambda: {
            "per_strategy": {
                "alpha": {
                    "budget": {
                        "limit_usdt": 1_000.0,
                        "used_today_usdt": 400.0,
                        "remaining_usdt": 600.0,
                    }
                },
                "beta": {
                    "budget": {
                        "limit_usdt": None,
                        "used_today_usdt": 0.0,
                        "remaining_usdt": None,
                    }
                },
            }
        },
    )

    response = client.get("/api/ui/ops_report", headers=ops_report_environment["viewer"])
    assert response.status_code == 200
    payload = response.json()

    assert isinstance(payload["badges"], dict)
    assert payload["open_trades_count"] >= 1
    assert payload["max_open_trades_limit"] == 8
    actions_payload = payload["last_audit_actions"]
    assert len(actions_payload) == len(actions)
    assert actions_payload[0]["operator"] == "alice"
    assert actions_payload[1]["operator"] == "bob"
    assert actions_payload[0]["details"] == {"status": "ok"}
    assert actions_payload[1]["details"] == "snapshot"

    budgets = payload["budgets"]
    assert [entry["strategy"] for entry in budgets] == ["alpha", "beta"]
    assert budgets[0]["budget_usdt"] == pytest.approx(1_000.0)
    assert budgets[0]["used_usdt"] == pytest.approx(400.0)
    assert budgets[0]["remaining_usdt"] == pytest.approx(600.0)
    assert budgets[1]["budget_usdt"] is None
    assert budgets[1]["remaining_usdt"] is None
