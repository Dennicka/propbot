import csv
import io
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.strategy_budget import StrategyBudgetManager, reset_strategy_budget_manager_for_tests
from app.strategy_risk import get_strategy_risk_manager, reset_strategy_risk_manager_for_tests
from app.strategy_pnl import reset_state_for_tests as reset_strategy_pnl_state
from positions import create_position, reset_positions


class _DummyAutopilot:
    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "last_action": "idle",
            "last_reason": None,
            "target_mode": "HOLD",
            "target_safe_mode": True,
            "armed": False,
            "last_decision": "ready",
            "last_decision_reason": None,
            "last_decision_ts": "2024-01-01T02:00:00+00:00",
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
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("STRATEGY_PNL_STATE_PATH", str(tmp_path / "strategy_pnl.json"))
    monkeypatch.setenv("POSITIONS_STORE_PATH", str(tmp_path / "positions.json"))

    reset_strategy_pnl_state()
    reset_positions()
    reset_strategy_risk_manager_for_tests()
    budget_manager = reset_strategy_budget_manager_for_tests(
        StrategyBudgetManager(
            initial_budgets={
                "alpha": {
                    "max_notional_usdt": 1_000.0,
                    "max_open_positions": 3,
                    "current_notional_usdt": 0.0,
                    "current_open_positions": 0,
                }
            }
        )
    )

    create_position(
        symbol="BTCUSDT",
        long_venue="binance",
        short_venue="okx",
        notional_usdt=1_000.0,
        entry_spread_bps=12.0,
        leverage=2.0,
        entry_long_price=30_000.0,
        entry_short_price=30_100.0,
        status="open",
        simulated=False,
        strategy="alpha",
    )

    risk_manager = get_strategy_risk_manager()
    risk_manager.record_fill("alpha", -50.0)
    risk_manager.record_failure("alpha", "spread_below_threshold")

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

    try:
        yield {
            "viewer": {"Authorization": "Bearer BBB"},
            "operator": {"Authorization": "Bearer AAA"},
        }
    finally:
        reset_strategy_budget_manager_for_tests()
        reset_strategy_risk_manager_for_tests()
        reset_strategy_pnl_state()
        reset_positions()


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
    assert payload["autopilot"]["last_decision"] == "ready"
    assert payload["pnl"]["unrealized_pnl_usdt"] == 42.0
    assert payload["positions_snapshot"]["exposure"]["binance"]["net_usdt"] == 50.0
    assert payload["audit"]["operator_actions"][0]["action"] == "TRIGGER_HOLD"

    assert "strategy_status" in payload
    alpha_status = payload["strategy_status"]["alpha"]
    assert alpha_status["strategy"] == "alpha"
    assert alpha_status["budget_blocked"] is True
    assert alpha_status["consecutive_failures"] >= 1

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
        row["section"] == "strategy_status:alpha"
        and row["key"] == "budget_blocked"
        and row["value"] == "True"
        for row in rows
    )
