"""Integration tests for the per-strategy PnL tracking."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import strategy_pnl
from app.strategy_risk import get_strategy_risk_manager, reset_strategy_risk_manager_for_tests


class _Autopilot:
    def as_dict(self) -> dict[str, object]:
        return {"enabled": False, "last_action": "idle", "target_mode": "HOLD", "target_safe_mode": True}


class _Safety:
    def as_dict(self) -> dict[str, object]:
        return {
            "hold_active": False,
            "hold_reason": "",
            "hold_source": "",
            "hold_since": None,
            "last_released_ts": None,
            "resume_request": None,
        }


def _build_dummy_state() -> SimpleNamespace:
    control = SimpleNamespace(
        mode="TRADE",
        safe_mode=False,
        dry_run=False,
        dry_run_mode=False,
        two_man_rule=False,
        flags={"MODE": "trade"},
    )
    return SimpleNamespace(control=control, autopilot=_Autopilot(), safety=_Safety())


@pytest.mark.usefixtures("client")
def test_strategy_pnl_and_ops_report(monkeypatch, tmp_path, client) -> None:
    pnl_state_path = tmp_path / "strategy_pnl.json"
    runtime_state_path = tmp_path / "runtime_state.json"
    monkeypatch.setenv("STRATEGY_PNL_STATE_PATH", str(pnl_state_path))
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(runtime_state_path))
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)

    strategy_pnl.reset_state_for_tests()
    reset_strategy_risk_manager_for_tests()
    manager = get_strategy_risk_manager()
    manager.limits.setdefault("cross_exchange_arb", {})["daily_loss_usdt"] = 150.0

    manager.record_fill("cross_exchange_arb", -100.0)
    manager.record_fill("cross_exchange_arb", -100.0)

    pnl_snapshot = strategy_pnl.snapshot("cross_exchange_arb")
    assert pnl_snapshot["realized_pnl_today"] == pytest.approx(-200.0)
    assert pnl_snapshot["realized_pnl_total"] == pytest.approx(-200.0)
    assert manager.is_frozen("cross_exchange_arb") is True

    monkeypatch.setattr("app.services.ops_report.runtime.get_state", _build_dummy_state)
    monkeypatch.setattr("app.services.ops_report.list_positions", lambda: [])

    async def _fake_positions_snapshot(_state, _positions):
        return {"positions": [], "exposure": {}, "totals": {}}

    monkeypatch.setattr("app.services.ops_report.build_positions_snapshot", _fake_positions_snapshot)
    monkeypatch.setattr("app.services.ops_report.build_pnl_snapshot", lambda _snapshot: {})
    monkeypatch.setattr(
        "app.services.ops_report.get_strategy_budget_manager",
        lambda: SimpleNamespace(snapshot=lambda: {"cross_exchange_arb": {"blocked": False}}),
    )
    monkeypatch.setattr("app.services.ops_report.list_recent_operator_actions", lambda limit=10: [])
    monkeypatch.setattr("app.services.ops_report.list_recent_events", lambda limit=10: [])

    response = client.get("/api/ui/ops_report")
    assert response.status_code == 200
    payload = response.json()
    per_strategy = payload.get("per_strategy_pnl", {})
    assert "cross_exchange_arb" in per_strategy
    arb_entry = per_strategy["cross_exchange_arb"]
    assert arb_entry["realized_pnl_today"] == pytest.approx(-200.0)
    assert arb_entry["realized_pnl_total"] == pytest.approx(-200.0)
    assert arb_entry["frozen"] is True
    assert arb_entry["budget_blocked"] is False
