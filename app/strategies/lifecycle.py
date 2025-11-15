from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.strategies.registry import (
    StrategyId,
    StrategyMode,
    get_strategy_registry,
)


@dataclass(slots=True)
class StrategyLifecycleContext:
    """Runtime inputs used when evaluating lifecycle rules for a strategy."""

    strategy_id: StrategyId
    runtime_profile: str  # "paper" | "testnet" | "live"
    router_flagset: Mapping[str, bool] | None = None
    extra: Mapping[str, object] | None = None


@dataclass(slots=True)
class StrategyLifecycleDecision:
    """Outcome of the lifecycle guard describing trade eligibility."""

    allowed: bool
    reason: str | None = None
    mode: StrategyMode | None = None
    priority: int | None = None


def check_strategy_lifecycle(ctx: StrategyLifecycleContext) -> StrategyLifecycleDecision:
    """Apply lifecycle guards to decide if routing is allowed for a strategy."""

    reg = get_strategy_registry()
    info = reg.get(ctx.strategy_id)
    if info is None:
        return StrategyLifecycleDecision(
            allowed=True,
            reason=f"strategy_id={ctx.strategy_id!r} not found in registry",
            mode=None,
            priority=None,
        )

    if not info.enabled:
        return StrategyLifecycleDecision(
            allowed=False,
            reason="strategy disabled",
            mode=info.mode,
            priority=info.priority,
        )

    if ctx.runtime_profile == "paper":
        return StrategyLifecycleDecision(
            allowed=True,
            reason="paper profile (all strategies allowed)",
            mode=info.mode,
            priority=info.priority,
        )

    if ctx.runtime_profile in {"testnet", "live"}:
        if ctx.runtime_profile == "live" and info.mode == "sandbox":
            return StrategyLifecycleDecision(
                allowed=False,
                reason="sandbox strategy cannot trade in live profile",
                mode=info.mode,
                priority=info.priority,
            )

    return StrategyLifecycleDecision(
        allowed=True,
        reason=None,
        mode=info.mode,
        priority=info.priority,
    )
