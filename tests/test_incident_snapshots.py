from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from app.exchange_watchdog import get_exchange_watchdog
from app.services import runtime
from app.strategy_budget import StrategyBudgetManager, reset_strategy_budget_manager_for_tests


@pytest.fixture
def incident_module(monkeypatch, tmp_path):
    monkeypatch.setenv("INCIDENT_MODE_ENABLED", "true")
    import app.incident.snapshots as snapshots

    module = importlib.reload(snapshots)
    monkeypatch.setattr(module, "SNAPSHOT_DIR", tmp_path)
    return module


def _configure_runtime_state() -> None:
    runtime.set_mode("RUN")
    runtime.apply_control_snapshot(
        {
            "safe_mode": False,
            "dry_run": False,
            "dry_run_mode": False,
            "two_man_rule": True,
            "auto_loop": True,
            "loop_pair": "ETHUSDT",
            "loop_venues": ["binance", "okx"],
            "order_notional_usdt": 250.0,
            "max_slippage_bps": 3,
            "min_spread_bps": 1.0,
            "poll_interval_sec": 7,
            "post_only": False,
            "reduce_only": True,
            "taker_fee_bps_binance": 5,
            "taker_fee_bps_okx": 7,
            "approvals": {"alice": "approved"},
            "preflight_passed": True,
            "deployment_mode": "paper",
            "environment": "paper",
        }
    )
    runtime.apply_risk_limits_snapshot(
        {
            "max_position_usdt": {"BTCUSDT": 1000.0},
            "max_open_orders": {"binance": 3},
            "max_daily_loss_usdt": 250.0,
        }
    )


def test_save_and_load_snapshot_round_trip(monkeypatch, tmp_path, incident_module):
    manager = StrategyBudgetManager(
        initial_budgets={
            "alpha": {
                "max_notional_usdt": 1000.0,
                "max_open_positions": 3,
                "current_notional_usdt": 500.0,
                "current_open_positions": 2,
            }
        }
    )
    reset_strategy_budget_manager_for_tests(manager)
    _configure_runtime_state()
    watchdog = get_exchange_watchdog()
    original_watchdog = watchdog.get_state()
    snapshot_watchdog = {
        "binance": {
            "ok": True,
            "status": "OK",
            "auto_hold": False,
            "last_check_ts": 1.0,
            "reason": "",
        },
        "okx": {
            "ok": False,
            "status": "DEGRADED",
            "auto_hold": False,
            "last_check_ts": 2.0,
            "reason": "degraded",
        },
    }
    watchdog.restore_snapshot(snapshot_watchdog)
    try:
        path = incident_module.save_snapshot(note="round trip")
        saved_payload = json.loads(Path(path).read_text(encoding="utf-8"))
        # Mutate runtime state to ensure load restores values
        runtime.set_mode("HOLD")
        runtime.apply_control_snapshot(
            {
                "safe_mode": True,
                "dry_run": True,
                "dry_run_mode": True,
                "two_man_rule": False,
                "auto_loop": False,
                "loop_pair": "BTCUSDT",
                "loop_venues": ["bybit"],
                "order_notional_usdt": 50.0,
                "max_slippage_bps": 1,
                "min_spread_bps": 0.5,
                "poll_interval_sec": 3,
                "post_only": True,
                "reduce_only": False,
                "taker_fee_bps_binance": 1,
                "taker_fee_bps_okx": 1,
                "approvals": {},
                "preflight_passed": False,
                "deployment_mode": "paper",
                "environment": "paper",
            }
        )
        runtime.apply_risk_limits_snapshot(
            {
                "max_position_usdt": {"BTCUSDT": 10.0},
                "max_open_orders": {"binance": 1},
                "max_daily_loss_usdt": 10.0,
            }
        )
        manager.apply_snapshot(
            {
                "alpha": {
                    "max_notional_usdt": 200.0,
                    "max_open_positions": 1,
                    "current_notional_usdt": 10.0,
                    "current_open_positions": 1,
                }
            }
        )
        watchdog.restore_snapshot({"binance": {"ok": True}})
        restored = incident_module.load_snapshot(path)
        state = runtime.get_state()
        control = state.control
        assert control.safe_mode == saved_payload["control"]["safe_mode"]
        assert control.auto_loop == saved_payload["control"]["auto_loop"]
        assert control.loop_pair == saved_payload["control"]["loop_pair"]
        assert control.loop_venues == saved_payload["control"]["loop_venues"]
        assert control.order_notional_usdt == saved_payload["control"]["order_notional_usdt"]
        assert runtime.get_state().risk.limits.as_dict() == saved_payload["risk_limits"]
        assert manager.snapshot() == saved_payload["budgets"]
        assert watchdog.get_state() == saved_payload["watchdog"]["exchanges"]
        assert restored["open_trades"]["count"] == saved_payload["open_trades"]["count"]
        assert Path(path).parent == incident_module.SNAPSHOT_DIR
    finally:
        watchdog.restore_snapshot(original_watchdog)
        reset_strategy_budget_manager_for_tests()


def test_snapshot_directory_lru(monkeypatch, tmp_path, incident_module):
    manager = StrategyBudgetManager(initial_budgets={"alpha": {}})
    reset_strategy_budget_manager_for_tests(manager)
    _configure_runtime_state()
    watchdog = get_exchange_watchdog()
    original_watchdog = watchdog.get_state()
    watchdog.restore_snapshot({})
    monkeypatch.setattr(incident_module, "SNAPSHOT_LIMIT", 5)
    try:
        for _ in range(7):
            incident_module.save_snapshot(note="cleanup test")
        files = list(tmp_path.glob("*.json"))
        assert len(files) <= 5
    finally:
        watchdog.restore_snapshot(original_watchdog)
        reset_strategy_budget_manager_for_tests()
