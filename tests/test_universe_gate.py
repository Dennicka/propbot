import asyncio
from typing import Mapping

import pytest

from app.routers import arb
from app.services import runtime
from app.services.arbitrage import ExecutionReport, Plan, execute_plan_async


@pytest.fixture(autouse=True)
def _reset_universe_state(monkeypatch: pytest.MonkeyPatch):
    runtime.clear_universe_unknown_pairs()
    monkeypatch.delenv("ENFORCE_UNIVERSE", raising=False)
    yield
    runtime.clear_universe_unknown_pairs()


def test_universe_gate_allows_when_flag_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Mapping[str, object]] = {}

    def fake_risk_gate(intent: Mapping[str, object]) -> Mapping[str, object]:
        calls["intent"] = intent
        return {"allowed": True}

    monkeypatch.setenv("ENFORCE_UNIVERSE", "0")
    monkeypatch.setattr("app.routers.arb.check_pair_allowed", lambda _symbol: (False, "universe"))
    monkeypatch.setattr("app.routers.arb.risk_gate", fake_risk_gate)

    intent = {"symbol": "DOGEUSDT", "strategy": "test", "intent_notional": 100.0, "intent_open_positions": 1}
    result = arb._maybe_skip_for_risk(intent)

    assert result is None
    assert "intent" in calls and calls["intent"]["symbol"] == "DOGEUSDT"
    assert runtime.get_universe_unknown_pairs() == []


@pytest.mark.asyncio
async def test_universe_gate_blocks_unknown_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_UNIVERSE", "1")
    monkeypatch.setattr("app.services.arbitrage.check_pair_allowed", lambda _symbol: (False, "universe"))

    def fail_risk_gate(_intent: Mapping[str, object]) -> Mapping[str, object]:
        raise AssertionError("risk_gate should not be evaluated when universe blocks")

    monkeypatch.setattr("app.services.arbitrage.risk_gate", fail_risk_gate)
    monkeypatch.setattr("app.services.arbitrage.accounting_record_intent", lambda *_, **__: ({}, {"ok": True}))
    monkeypatch.setattr("app.services.arbitrage.accounting_record_fill", lambda *_, **__: {})
    monkeypatch.setattr("app.services.arbitrage.get_risk_accounting_snapshot", lambda: {})

    plan = Plan(
        symbol="DOGEUSDT",
        notional=100.0,
        used_slippage_bps=0,
        used_fees_bps={},
        viable=True,
    )
    report = await execute_plan_async(plan)

    assert isinstance(report, ExecutionReport)
    assert report.state == "SKIPPED_BY_RISK"
    assert report.risk_gate.get("reason") == "universe"
    assert report.risk_gate.get("details") == {"reason": "universe"}
    assert "DOGEUSDT" in runtime.get_universe_unknown_pairs()


@pytest.mark.asyncio
async def test_universe_gate_allows_known_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_UNIVERSE", "1")
    monkeypatch.setattr("app.services.arbitrage.check_pair_allowed", lambda symbol: (True, ""))

    calls: dict[str, object] = {}

    def fake_risk_gate(intent: Mapping[str, object]) -> Mapping[str, object]:
        calls["intent"] = intent
        return {"allowed": True}

    monkeypatch.setattr("app.services.arbitrage.risk_gate", fake_risk_gate)
    monkeypatch.setattr("app.services.arbitrage.accounting_record_intent", lambda *_, **__: ({}, {"ok": True}))
    monkeypatch.setattr("app.services.arbitrage.accounting_record_fill", lambda *_, **__: {})
    class DummyRouter:
        async def execute_plan(self, plan: Plan, *, allow_safe_mode: bool = False):
            calls["executed_plan"] = plan
            return {"pnl": {"total": 5.0}, "orders": [], "exposures": []}

    monkeypatch.setattr("app.services.arbitrage.ExecutionRouter", lambda: DummyRouter())
    monkeypatch.setattr("app.services.arbitrage.get_risk_accounting_snapshot", lambda: {})

    plan = Plan(
        symbol="BTCUSDT",
        notional=200.0,
        used_slippage_bps=0,
        used_fees_bps={},
        viable=True,
    )
    report = await execute_plan_async(plan)

    assert report.state == "DONE"
    assert pytest.approx(calls["intent"]["intent_notional"], rel=0.0) == 200.0
    assert calls["executed_plan"].symbol == "BTCUSDT"
    assert runtime.get_universe_unknown_pairs() == []
