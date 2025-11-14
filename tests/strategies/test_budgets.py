from __future__ import annotations

from decimal import Decimal

import pytest

from app.strategies.budgets import (
    StrategyBudgetCheckInput,
    check_strategy_budget,
)
from app.strategies.registry import StrategyInfo, StrategyRegistry


@pytest.fixture
def strategy_registry(monkeypatch: pytest.MonkeyPatch) -> StrategyRegistry:
    registry = StrategyRegistry()
    monkeypatch.setattr("app.strategies.registry._REGISTRY", registry)
    monkeypatch.setattr("app.strategies.registry.get_strategy_registry", lambda: registry)
    monkeypatch.setattr("app.strategies.budgets.get_strategy_registry", lambda: registry)
    return registry


def test_check_strategy_budget_allows_within_limits(strategy_registry: StrategyRegistry) -> None:
    strategy_registry.register(
        StrategyInfo(
            id="budget-alpha",
            name="Budget Alpha",
            description="test",
            tags=["test"],
            max_notional_usd=10_000.0,
            max_daily_loss_usd=500.0,
            max_open_positions=5,
        )
    )

    decision = check_strategy_budget(
        StrategyBudgetCheckInput(
            strategy_id="budget-alpha",
            notional_usd_after=Decimal("9000"),
            daily_pnl_usd=Decimal("-100"),
            open_positions_after=3,
        )
    )

    assert decision.allowed is True
    assert decision.breached_limit is None


def test_check_strategy_budget_blocks_notional(strategy_registry: StrategyRegistry) -> None:
    strategy_registry.register(
        StrategyInfo(
            id="budget-beta",
            name="Budget Beta",
            description="test",
            tags=["test"],
            max_notional_usd=1_000.0,
            max_daily_loss_usd=500.0,
            max_open_positions=5,
        )
    )

    decision = check_strategy_budget(
        StrategyBudgetCheckInput(
            strategy_id="budget-beta",
            notional_usd_after=Decimal("1500"),
        )
    )

    assert decision.allowed is False
    assert decision.breached_limit == "max_notional_usd"


def test_check_strategy_budget_blocks_daily_loss(strategy_registry: StrategyRegistry) -> None:
    strategy_registry.register(
        StrategyInfo(
            id="budget-gamma",
            name="Budget Gamma",
            description="test",
            tags=["test"],
            max_notional_usd=10_000.0,
            max_daily_loss_usd=500.0,
            max_open_positions=5,
        )
    )

    decision = check_strategy_budget(
        StrategyBudgetCheckInput(
            strategy_id="budget-gamma",
            notional_usd_after=Decimal("1000"),
            daily_pnl_usd=Decimal("-600"),
        )
    )

    assert decision.allowed is False
    assert decision.breached_limit == "max_daily_loss_usd"


def test_check_strategy_budget_unknown_strategy(strategy_registry: StrategyRegistry) -> None:
    decision = check_strategy_budget(
        StrategyBudgetCheckInput(strategy_id="unknown", notional_usd_after=Decimal("0"))
    )

    assert decision.allowed is True
    assert decision.breached_limit is None
    assert decision.reason is not None
    assert "not found" in decision.reason
