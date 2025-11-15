from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal


StrategyId = str
StrategyMode = Literal["sandbox", "canary", "live"]


@dataclass(slots=True)
class StrategyInfo:
    """Static metadata describing a trading strategy configuration."""

    id: StrategyId
    name: str
    description: str
    tags: List[str]
    max_notional_usd: float | None = None
    max_daily_loss_usd: float | None = None
    max_open_positions: int | None = None
    enabled: bool = True
    mode: StrategyMode = "sandbox"
    priority: int = 100


class StrategyRegistry:
    """In-memory container of strategies available to the trading system."""

    def __init__(self) -> None:
        self._by_id: Dict[StrategyId, StrategyInfo] = {}

    def register(self, info: StrategyInfo) -> None:
        self._by_id[info.id] = info

    def get(self, strategy_id: StrategyId) -> StrategyInfo | None:
        return self._by_id.get(strategy_id)

    def require(self, strategy_id: StrategyId) -> StrategyInfo:
        info = self.get(strategy_id)
        if info is None:
            raise KeyError(f"Unknown strategy_id={strategy_id!r}")
        return info

    def all(self) -> list[StrategyInfo]:
        return list(self._by_id.values())


_REGISTRY = StrategyRegistry()


def get_strategy_registry() -> StrategyRegistry:
    """Return the process-wide strategy registry instance."""

    return _REGISTRY


def register_default_strategies() -> None:
    """Populate the global registry with the default strategy set."""

    reg = get_strategy_registry()

    reg.register(
        StrategyInfo(
            id="xex_arb",
            name="Cross-exchange arbitrage",
            description="Simple cross-exchange perp arbitrage (long/short on two venues).",
            tags=["arb", "xex", "perp"],
            max_notional_usd=50_000.0,
            max_daily_loss_usd=1_000.0,
            max_open_positions=10,
            enabled=True,
            mode="sandbox",
            priority=50,
        )
    )
    reg.register(
        StrategyInfo(
            id="test_strategy",
            name="Test / Sandbox strategy",
            description="Lightweight strategy for smoke/testing.",
            tags=["test"],
            max_notional_usd=None,
            max_daily_loss_usd=None,
            max_open_positions=None,
            enabled=True,
            mode="sandbox",
            priority=100,
        )
    )
