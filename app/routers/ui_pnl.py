from __future__ import annotations
from decimal import Decimal
from typing import Iterable, Tuple

from fastapi import APIRouter
from pydantic import BaseModel

from ..pnl.models import (
    PortfolioPnlSnapshot,
    PositionPnlSnapshot,
    aggregate_portfolio_pnl,
    build_strategy_pnl_snapshots,
)
from ..strategies.registry import get_strategy_registry


router = APIRouter()


class UiPositionPnl(BaseModel):
    symbol: str
    venue: str
    size: Decimal
    entry_price: Decimal
    mark_price: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    funding_paid: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal


class UiStrategyPnl(BaseModel):
    strategy_id: str
    strategy_name: str | None = None
    gross_pnl: Decimal
    net_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    funding_paid: Decimal
    positions_count: int
    notional_usd: Decimal


class UiPortfolioPnl(BaseModel):
    realized: Decimal
    unrealized: Decimal
    fees_paid: Decimal
    funding_paid: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    positions: Tuple[UiPositionPnl, ...]
    by_strategy: Tuple[UiStrategyPnl, ...] = ()

    @classmethod
    def from_snapshot(
        cls,
        snapshot: PortfolioPnlSnapshot,
        *,
        strategy_snapshots: Iterable[UiStrategyPnl] | None = None,
    ) -> "UiPortfolioPnl":
        positions = tuple(UiPositionPnl(**position.__dict__) for position in snapshot.positions)
        by_strategy = tuple(strategy_snapshots or ())
        return cls(
            realized=snapshot.realized_pnl,
            unrealized=snapshot.unrealized_pnl,
            fees_paid=snapshot.fees_paid,
            funding_paid=snapshot.funding_paid,
            gross_pnl=snapshot.gross_pnl,
            net_pnl=snapshot.net_pnl,
            positions=positions,
            by_strategy=by_strategy,
        )


def _load_position_snapshots() -> Iterable[PositionPnlSnapshot]:
    """Placeholder data-source until real accounting is wired in."""

    return ()


def _build_strategy_ui_models(
    positions: Iterable[PositionPnlSnapshot],
) -> Tuple[UiStrategyPnl, ...]:
    snapshots = build_strategy_pnl_snapshots(positions)
    if not snapshots:
        return ()

    registry = get_strategy_registry()
    ui_snapshots = []
    for snapshot in snapshots:
        info = registry.get(snapshot.strategy_id)
        ui_snapshots.append(
            UiStrategyPnl(
                strategy_id=snapshot.strategy_id,
                strategy_name=info.name if info is not None else None,
                gross_pnl=snapshot.gross_pnl,
                net_pnl=snapshot.net_pnl,
                realized_pnl=snapshot.realized_pnl,
                unrealized_pnl=snapshot.unrealized_pnl,
                fees_paid=snapshot.fees_paid,
                funding_paid=snapshot.funding_paid,
                positions_count=snapshot.positions_count,
                notional_usd=snapshot.notional_usd,
            )
        )

    return tuple(ui_snapshots)


@router.get("/pnl", response_model=UiPortfolioPnl)
def pnl() -> UiPortfolioPnl:
    positions = tuple(_load_position_snapshots())
    snapshot = aggregate_portfolio_pnl(positions)
    strategy_models = _build_strategy_ui_models(positions)
    return UiPortfolioPnl.from_snapshot(snapshot, strategy_snapshots=strategy_models)
