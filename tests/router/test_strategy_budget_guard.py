from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.router.smart_router import SmartRouter
from app.strategies.budgets import StrategyBudgetDecision
from app.strategies.registry import StrategyInfo, StrategyRegistry


def test_router_blocks_on_strategy_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.router.smart_router.ff.md_watchdog_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.pretrade_strict_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.risk_limits_on", lambda: False)
    monkeypatch.setattr(
        "app.router.smart_router.SafeMode.is_active", classmethod(lambda cls: False)
    )
    monkeypatch.setenv("LIVE_CONFIRM", "I_UNDERSTAND")
    monkeypatch.setenv("READINESS_OK", "1")

    registry = StrategyRegistry()
    registry.register(
        StrategyInfo(
            id="strategy-budget-test",
            name="Strategy Budget Test",
            description="test",
            tags=["test"],
            max_notional_usd=500.0,
            max_daily_loss_usd=100.0,
            max_open_positions=3,
        )
    )
    monkeypatch.setattr("app.strategies.registry._REGISTRY", registry)
    monkeypatch.setattr("app.strategies.registry.get_strategy_registry", lambda: registry)
    monkeypatch.setattr("app.router.smart_router.get_strategy_registry", lambda: registry)

    dummy_watchdog = SimpleNamespace(mark_router_activity=lambda: None)
    monkeypatch.setattr("app.router.smart_router.get_watchdog", lambda: dummy_watchdog)

    profile = SimpleNamespace(name="test", is_canary=False)
    monkeypatch.setattr("app.router.smart_router.get_profile", lambda: profile)
    monkeypatch.setattr("app.router.smart_router.is_live", lambda prof: False)

    captured_input: dict[str, object] = {}

    def fake_check(inp):
        captured_input["inp"] = inp
        return StrategyBudgetDecision(
            allowed=False,
            reason="strategy notional limit exceeded",
            breached_limit="max_notional_usd",
        )

    monkeypatch.setattr("app.router.smart_router.check_strategy_budget", fake_check)

    state = SimpleNamespace(config=None)
    market = SimpleNamespace()
    router = SmartRouter(state=state, market_data=market)

    response = router.register_order(
        strategy="strategy-budget-test",
        venue="demo",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts_ns=1,
        nonce=1,
    )

    assert response["status"] == "strategy_budget_blocked"
    assert response["reason"] == "strategy-budget"
    assert response["detail"] == "strategy notional limit exceeded"
    assert response["breached_limit"] == "max_notional_usd"

    captured = captured_input.get("inp")
    assert captured is not None
    assert captured.strategy_id == "strategy-budget-test"
    assert float(captured.notional_usd_after) == pytest.approx(100.0)
