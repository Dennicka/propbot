from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from app.strategies.registry import StrategyId, StrategyInfo, get_strategy_registry


@dataclass(slots=True)
class StrategyBudgetCheckInput:
    strategy_id: StrategyId
    notional_usd_after: Decimal
    daily_pnl_usd: Decimal | None = None
    open_positions_after: int | None = None
    profile: str | None = None
    context: Mapping[str, object] | None = None


@dataclass(slots=True)
class StrategyBudgetDecision:
    allowed: bool
    reason: str | None = None
    breached_limit: str | None = None


def check_strategy_budget(inp: StrategyBudgetCheckInput) -> StrategyBudgetDecision:
    reg = get_strategy_registry()
    info: StrategyInfo | None = reg.get(inp.strategy_id)
    if info is None:
        return StrategyBudgetDecision(
            allowed=True,
            reason=f"strategy_id={inp.strategy_id!r} not found in registry",
            breached_limit=None,
        )

    if info.max_notional_usd is not None:
        try:
            notional_after = float(inp.notional_usd_after)
        except (ArithmeticError, ValueError):
            notional_after = 0.0
        if notional_after > info.max_notional_usd:
            return StrategyBudgetDecision(
                allowed=False,
                reason="strategy notional limit exceeded",
                breached_limit="max_notional_usd",
            )

    if info.max_daily_loss_usd is not None and inp.daily_pnl_usd is not None:
        try:
            daily_pnl_loss = float(-inp.daily_pnl_usd)
        except (ArithmeticError, ValueError):
            daily_pnl_loss = 0.0
        if daily_pnl_loss > info.max_daily_loss_usd:
            return StrategyBudgetDecision(
                allowed=False,
                reason="strategy daily loss limit exceeded",
                breached_limit="max_daily_loss_usd",
            )

    if info.max_open_positions is not None and inp.open_positions_after is not None:
        if inp.open_positions_after > info.max_open_positions:
            return StrategyBudgetDecision(
                allowed=False,
                reason="strategy open positions limit exceeded",
                breached_limit="max_open_positions",
            )

    return StrategyBudgetDecision(allowed=True, reason=None, breached_limit=None)
