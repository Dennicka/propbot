from __future__ import annotations

import pytest

from app.strategies.lifecycle import (
    StrategyLifecycleContext,
    check_strategy_lifecycle,
)
from app.strategies.registry import (
    StrategyInfo,
    StrategyRegistry,
)


@pytest.fixture
def strategy_registry(monkeypatch: pytest.MonkeyPatch) -> StrategyRegistry:
    registry = StrategyRegistry()
    monkeypatch.setattr("app.strategies.registry._REGISTRY", registry)
    monkeypatch.setattr("app.strategies.registry.get_strategy_registry", lambda: registry)
    monkeypatch.setattr("app.strategies.lifecycle.get_strategy_registry", lambda: registry)
    return registry


def test_lifecycle_allows_enabled_strategy_in_paper(
    strategy_registry: StrategyRegistry,
) -> None:
    strategy_registry.register(
        StrategyInfo(
            id="test_strategy",
            name="Test Strategy",
            description="test",
            tags=["test"],
            enabled=True,
            mode="sandbox",
        )
    )

    ctx = StrategyLifecycleContext(strategy_id="test_strategy", runtime_profile="paper")
    decision = check_strategy_lifecycle(ctx)

    assert decision.allowed is True
    assert decision.mode == "sandbox"


def test_lifecycle_blocks_disabled_strategy(
    strategy_registry: StrategyRegistry,
) -> None:
    strategy_registry.register(
        StrategyInfo(
            id="disabled_strategy",
            name="Disabled Strategy",
            description="test",
            tags=["test"],
            enabled=False,
        )
    )

    ctx = StrategyLifecycleContext(strategy_id="disabled_strategy", runtime_profile="testnet")
    decision = check_strategy_lifecycle(ctx)

    assert decision.allowed is False
    assert decision.reason is not None
    assert "disabled" in decision.reason


def test_lifecycle_blocks_sandbox_strategy_in_live_profile(
    strategy_registry: StrategyRegistry,
) -> None:
    strategy_registry.register(
        StrategyInfo(
            id="sandbox_strategy",
            name="Sandbox Strategy",
            description="test",
            tags=["test"],
            enabled=True,
            mode="sandbox",
        )
    )

    ctx = StrategyLifecycleContext(strategy_id="sandbox_strategy", runtime_profile="live")
    decision = check_strategy_lifecycle(ctx)

    assert decision.allowed is False
    assert decision.reason is not None
    assert "sandbox" in decision.reason


def test_lifecycle_allows_live_strategy_in_live_profile(
    strategy_registry: StrategyRegistry,
) -> None:
    strategy_registry.register(
        StrategyInfo(
            id="live_strategy",
            name="Live Strategy",
            description="test",
            tags=["test"],
            enabled=True,
            mode="live",
        )
    )

    ctx = StrategyLifecycleContext(strategy_id="live_strategy", runtime_profile="live")
    decision = check_strategy_lifecycle(ctx)

    assert decision.allowed is True
    assert decision.mode == "live"
