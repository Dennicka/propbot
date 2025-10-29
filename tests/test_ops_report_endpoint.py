"""Tests for the ops report API endpoints."""

from __future__ import annotations

import csv
import io
import json
from types import SimpleNamespace
from typing import Any

import pytest


class _DummyAutopilot:
    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "last_action": "idle",
            "target_mode": "HOLD",
            "target_safe_mode": True,
        }


class _DummySafety:
    def as_dict(self) -> dict[str, Any]:
        return {
            "hold_active": True,
            "hold_reason": "maintenance",
            "hold_source": "ops",
            "hold_since": "2024-01-01T00:00:00+00:00",
            "last_released_ts": None,
            "resume_request": {"pending": True, "requested_by": "alice"},
        }


class _DummyStrategyManager:
    def full_snapshot(self) -> dict[str, Any]:
        return {
            "strategies": {
                "alpha": {
                    "enabled": True,
                    "frozen": True,
                    "freeze_reason": "limit_breach",
                    "breach": True,
                    "breach_reasons": ["limit_exceeded"],
                    "limits": {"max_notional": 1_000.0},
                    "state": {
                        "frozen": True,
                        "freeze_reason": "limit_breach",
                        "consecutive_failures": 2,
                    },
                }
            }
        }


@pytest.fixture
def ops_report_environment(monkeypatch, tmp_path):
    secrets_payload = {
        "operator_tokens": {
            "alice": {"token": "AAA", "role": "operator"},
            "bob": {"token": "BBB", "role": "viewer"},
        },
        "approve_token": "ZZZ",
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))

    dummy_state = SimpleNamespace(
        control=SimpleNamespace(
            mode="HOLD",
            safe_mode=True,
            dry_run=True,
            dry_run_mode=False,
            two_man_rule=True,
            flags={
                "MODE": "paper",
                "SAFE_MODE": True,
                "DRY_RUN": True,
            },
        ),
        autopilot=_DummyAutopilot(),
        safety=_DummySafety(),
    )

    async def fake_positions_snapshot(_state, _positions):
        return {
            "positions": [{"id": "pos-1", "status": "open", "legs": []}],
            "exposure": {
                "binance": {
                    "long_notional": 100.0,
                    "short_notional": 50.0,
                    "net_usdt": 50.0,
                }
            },
            "totals": {"unrealized_pnl_usdt": 12.34},
        }

    monkeypatch.setattr("app.services.ops_report.runtime.get_state", lambda: dummy_state)
    monkeypatch.setattr("app.services.ops_report.list_positions", lambda: [{"id": "pos-1"}])
    monkeypatch.setattr(
        "app.services.ops_report.build_positions_snapshot",
        fake_positions_snapshot,
    )
    monkeypatch.setattr(
        "app.services.ops_report.build_pnl_snapshot",
        lambda _snapshot: {
            "unrealized_pnl_usdt": 42.0,
            "realised_pnl_today_usdt": 7.0,
            "total_exposure_usdt": 150.0,
        },
    )
    monkeypatch.setattr(
        "app.services.ops_report.get_strategy_risk_manager",
        lambda: _DummyStrategyManager(),
    )
    monkeypatch.setattr(
        "app.services.ops_report.get_strategy_budget_manager",
        lambda: SimpleNamespace(snapshot=lambda: {"alpha": {"blocked": True}}),
    )
    monkeypatch.setattr(
        "app.services.ops_report.snapshot_strategy_pnl",
        lambda: {
            "alpha": {
                "realized_pnl_today": -12.5,
                "realized_pnl_total": 87.5,
                "realized_pnl_7d": -5.0,
                "max_drawdown_observed": 42.0,
            }
        },
    )
    monkeypatch.setattr(
        "app.services.ops_report.list_recent_operator_actions",
        lambda limit=10: [
            {
                "timestamp": "2024-01-01T00:00:00+00:00",
                "operator_name": "alice",
                "role": "operator",
                "action": "TRIGGER_HOLD",
                "details": {"status": "ok"},
            }
        ],
    )
    monkeypatch.setattr(
        "app.services.ops_report.list_recent_events",
        lambda limit=10: [
            {
                "timestamp": "2024-01-01T01:00:00+00:00",
                "actor": "system",
                "action": "Safety hold engaged",
                "status": "applied",
                "reason": "limit_breach",
            }
        ],
    )

    return {
        "viewer": {"Authorization": "Bearer BBB"},
        "operator": {"Authorization": "Bearer AAA"},
    }


def test_ops_report_requires_token_when_auth_enabled(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret")

    response = client.get("/api/ui/ops_report")
    assert response.status_code == 401


def test_ops_report_json_accessible_for_viewer_and_operator(
    client, ops_report_environment
) -> None:
    response = client.get("/api/ui/ops_report", headers=ops_report_environment["viewer"])
    assert response.status_code == 200
    payload = response.json()

    assert payload["runtime"]["mode"] == "HOLD"
    assert payload["runtime"]["safety"]["hold_reason"] == "maintenance"
    assert payload["pnl"]["unrealized_pnl_usdt"] == 42.0
    assert payload["positions_snapshot"]["exposure"]["binance"]["net_usdt"] == 50.0
    assert payload["strategy_controls"]["alpha"]["freeze_reason"] == "limit_breach"
    assert payload["audit"]["operator_actions"][0]["action"] == "TRIGGER_HOLD"
    assert "per_strategy_pnl" in payload
    alpha_pnl = payload["per_strategy_pnl"]["alpha"]
    assert alpha_pnl["realized_pnl_today"] == -12.5
    assert alpha_pnl["frozen"] is True
    assert alpha_pnl["budget_blocked"] is True

    operator_response = client.get(
        "/api/ui/ops_report",
        headers=ops_report_environment["operator"],
    )
    assert operator_response.status_code == 200
    assert operator_response.json()["audit"]["ops_events"]


def test_ops_report_csv_export(client, ops_report_environment) -> None:
    response = client.get(
        "/api/ui/ops_report.csv",
        headers=ops_report_environment["viewer"],
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows
    assert any(row["section"] == "runtime" and row["key"] == "mode" for row in rows)
    assert any(
        row["section"] == "strategy:alpha"
        and row["key"] == "freeze_reason"
        and row["value"] == "limit_breach"
        for row in rows
    )
    assert any(
        row["section"] == "strategy_pnl:alpha"
        and row["key"] == "frozen"
        and row["value"] == "True"
        for row in rows
    )
