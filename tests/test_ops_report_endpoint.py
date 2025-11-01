import csv
import io
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.risk import accounting as risk_accounting, core as risk_core
from app.strategy_budget import StrategyBudgetManager, reset_strategy_budget_manager_for_tests
from app.strategy_risk import get_strategy_risk_manager, reset_strategy_risk_manager_for_tests
from app.strategy_pnl import reset_state_for_tests as reset_strategy_pnl_state
from positions import create_position, reset_positions

from app.services import runtime
from app.watchdog.exchange_watchdog import (
    ExchangeWatchdog,
    get_exchange_watchdog,
    reset_exchange_watchdog_for_tests,
)


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
            "carol": {"token": "CCC", "role": "auditor"},
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
    monkeypatch.setenv("DAILY_LOSS_CAP_USDT", "200")
    monkeypatch.setenv("ENFORCE_DAILY_LOSS_CAP", "1")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")
    runtime.clear_universe_unknown_pairs()
    runtime.record_universe_unknown_pair("DOGEUSDT")

    reset_strategy_pnl_state()
    reset_positions()
    reset_strategy_risk_manager_for_tests()
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()
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
    risk_accounting.record_fill("alpha", 0.0, -80.0, simulated=False)

    reset_exchange_watchdog_for_tests()
    watchdog = get_exchange_watchdog()
    watchdog.check_once(lambda: {"binance": {"ok": False, "reason": "timeout"}})
    monkeypatch.setattr(
        "app.services.ops_report.get_exchange_watchdog", lambda: watchdog
    )

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
    async def fake_pnl_attribution() -> dict[str, object]:
        return {
            "generated_at": "2024-01-01T02:00:00+00:00",
            "by_strategy": {
                "alpha": {
                    "realized": 42.0,
                    "unrealized": 3.0,
                    "fees": 0.5,
                    "rebates": 0.1,
                    "funding": 1.0,
                    "net": 45.6,
                }
            },
            "by_venue": {
                "binance": {
                    "realized": 40.0,
                    "unrealized": 2.5,
                    "fees": 0.4,
                    "rebates": 0.05,
                    "funding": 0.5,
                    "net": 42.65,
                }
            },
            "totals": {
                "realized": 42.0,
                "unrealized": 3.0,
                "fees": 0.5,
                "rebates": 0.1,
                "funding": 1.0,
                "net": 45.6,
            },
            "meta": {"exclude_simulated": True},
        }

    monkeypatch.setattr("app.services.ops_report.build_pnl_attribution", fake_pnl_attribution)
    monkeypatch.setattr(
        "app.services.ops_report.get_risk_accounting_snapshot",
        lambda: {
            "per_strategy": {
                "alpha": {
                    "budget": {
                        "limit_usdt": 1_000.0,
                        "used_today_usdt": 250.0,
                        "remaining_usdt": 750.0,
                    }
                }
            }
        },
    )

    try:
        yield {
            "viewer": {"Authorization": "Bearer BBB"},
            "operator": {"Authorization": "Bearer AAA"},
            "auditor": {"Authorization": "Bearer CCC"},
        }
    finally:
        runtime.clear_universe_unknown_pairs()
        reset_exchange_watchdog_for_tests()
        reset_strategy_budget_manager_for_tests()
        reset_strategy_risk_manager_for_tests()
        reset_strategy_pnl_state()
        reset_positions()


def test_ops_report_requires_token_when_auth_enabled(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret")

    response = client.get("/api/ui/ops_report")
    assert response.status_code == 401


def test_ops_report_json_accessible_for_roles(
    client, ops_report_environment
) -> None:
    response = client.get("/api/ui/ops_report", headers=ops_report_environment["viewer"])
    assert response.status_code == 200
    payload = response.json()

    assert payload["open_trades_count"] == 1
    assert payload["max_open_trades_limit"] == 5
    assert payload["runtime"]["mode"] == "HOLD"
    assert payload["runtime"]["safety"]["hold_reason"] == "maintenance"
    assert payload["autopilot"]["last_decision"] == "ready"
    assert payload["pnl"]["unrealized_pnl_usdt"] == 42.0
    assert payload["pnl_attribution"]["totals"]["realized"] == pytest.approx(42.0)
    assert payload["pnl_attribution"]["by_strategy"]["alpha"]["fees"] == pytest.approx(0.5)
    assert payload["positions_snapshot"]["exposure"]["binance"]["net_usdt"] == 50.0
    assert payload["audit"]["operator_actions"][0]["action"] == "TRIGGER_HOLD"
    assert payload["last_audit_actions"][0]["action"] == "TRIGGER_HOLD"
    assert payload["badges"]
    assert payload["budgets"][0]["strategy"] == "alpha"
    assert payload["universe_enforced"] is False
    assert payload["unknown_pairs"] == ["DOGEUSDT"]
    watchdog_payload = payload.get("watchdog")
    assert watchdog_payload["watchdog_ok"] is False
    assert watchdog_payload["overall_ok"] is False
    assert watchdog_payload["degraded_reasons"].get("binance") == "timeout"
    transitions = watchdog_payload.get("recent_transitions")
    assert isinstance(transitions, list)
    assert transitions
    daily_loss = payload["daily_loss_cap"]
    assert daily_loss["max_daily_loss_usdt"] == pytest.approx(200.0)
    assert daily_loss["losses_usdt"] == pytest.approx(80.0)
    assert daily_loss["breached"] is False

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

    auditor_response = client.get(
        "/api/ui/ops_report",
        headers=ops_report_environment["auditor"],
    )
    assert auditor_response.status_code == 200
    auditor_payload = auditor_response.json()
    assert auditor_payload["runtime"]["mode"] == "HOLD"
    assert auditor_payload["audit"]["operator_actions"][0]["action"] == "TRIGGER_HOLD"
    assert auditor_payload["daily_loss_cap"]["losses_usdt"] == pytest.approx(80.0)
    assert auditor_payload["unknown_pairs"] == ["DOGEUSDT"]


def test_ops_report_csv_export(client, ops_report_environment) -> None:
    response = client.get(
        "/api/ui/ops_report.csv",
        headers=ops_report_environment["viewer"],
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert rows
    header = rows[0].keys()
    assert "timestamp" in header
    assert "open_trades_count" in header
    assert "strategy" in header
    assert "attrib_scope" in header
    assert "attrib_net" in header
    first_row = rows[0]
    assert first_row["open_trades_count"] == "1"
    assert first_row["max_open_trades_limit"] == "5"
    assert first_row["strategy"] == "alpha"
    assert float(first_row["budget_usdt"]) == pytest.approx(1_000.0)
    assert float(first_row["used_usdt"]) == pytest.approx(250.0)
    assert float(first_row["remaining_usdt"]) == pytest.approx(750.0)
    assert first_row["daily_loss_status"]
    assert first_row["watchdog_status"]
    assert first_row["auto_trade"]

    totals_row = next((row for row in rows if row.get("attrib_scope") == "totals"), None)
    assert totals_row is not None
    assert totals_row["attrib_name"] == "totals"
    assert totals_row["attrib_realized"] == "42.0"
