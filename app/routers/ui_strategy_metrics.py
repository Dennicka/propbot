from __future__ import annotations

import asyncio
from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel

from ..pnl.strategy_metrics import build_strategy_performance, get_recent_trades
from ..pnl.models import StrategyPerformanceSnapshot
from ..strategies.registry import get_strategy_registry

router = APIRouter(prefix="/api/ui", tags=["ui"])


class UiStrategyPerformance(BaseModel):
    strategy_id: str
    strategy_name: str | None = None
    trades_count: int
    winning_trades: int
    losing_trades: int
    winrate: float
    gross_pnl: Decimal
    net_pnl: Decimal
    average_trade_pnl: Decimal
    turnover_notional: Decimal
    max_drawdown: Decimal | None = None


def _to_ui_model(
    snapshot: StrategyPerformanceSnapshot,
) -> UiStrategyPerformance:
    registry = get_strategy_registry()
    info = registry.get(snapshot.strategy_id)
    name = info.name if info is not None else None
    return UiStrategyPerformance(
        strategy_id=snapshot.strategy_id,
        strategy_name=name,
        trades_count=snapshot.trades_count,
        winning_trades=snapshot.winning_trades,
        losing_trades=snapshot.losing_trades,
        winrate=snapshot.winrate,
        gross_pnl=snapshot.gross_pnl,
        net_pnl=snapshot.net_pnl,
        average_trade_pnl=snapshot.average_trade_pnl,
        turnover_notional=snapshot.turnover_notional,
        max_drawdown=snapshot.max_drawdown,
    )


@router.get("/strategy-metrics", response_model=list[UiStrategyPerformance])
async def strategy_metrics() -> list[UiStrategyPerformance]:
    trades = await asyncio.to_thread(get_recent_trades)
    snapshots = build_strategy_performance(trades)
    return [_to_ui_model(snapshot) for snapshot in snapshots]


__all__ = ["router", "strategy_metrics", "UiStrategyPerformance"]
