from types import SimpleNamespace

import pytest

from app.router.smart_router import SmartRouter
from app.strategies.budgets import StrategyBudgetDecision
from app.strategies.registry import StrategyInfo, StrategyRegistry


def test_router_blocks_on_strategy_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
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
            id="lifecycle-test",
            name="Lifecycle Test",
            description="test",
            tags=["test"],
            enabled=False,
        )
    )
    monkeypatch.setattr("app.strategies.registry._REGISTRY", registry)
    monkeypatch.setattr("app.strategies.registry.get_strategy_registry", lambda: registry)
    monkeypatch.setattr("app.router.smart_router.get_strategy_registry", lambda: registry)
    monkeypatch.setattr("app.strategies.lifecycle.get_strategy_registry", lambda: registry)

    dummy_watchdog = SimpleNamespace(mark_router_activity=lambda: None)
    monkeypatch.setattr("app.router.smart_router.get_watchdog", lambda: dummy_watchdog)

    profile = SimpleNamespace(name="paper", is_canary=False)
    monkeypatch.setattr("app.router.smart_router.get_profile", lambda: profile)
    monkeypatch.setattr("app.router.smart_router.is_live", lambda prof: False)

    def allow_budget(_: object) -> StrategyBudgetDecision:
        return StrategyBudgetDecision(allowed=True, reason=None, breached_limit=None)

    monkeypatch.setattr("app.router.smart_router.check_strategy_budget", allow_budget)

    state = SimpleNamespace(config=None)
    market = SimpleNamespace()
    router = SmartRouter(state=state, market_data=market)

    response = router.register_order(
        strategy="lifecycle-test",
        venue="demo",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts_ns=1,
        nonce=1,
    )

    assert response["status"] == "strategy_lifecycle_blocked"
    assert response["reason"] == "strategy-lifecycle"
    assert "detail" in response
    assert "disabled" in (response["detail"] or "")
