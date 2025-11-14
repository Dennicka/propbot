from __future__ import annotations
from decimal import Decimal
from typing import Iterable, Tuple

from fastapi import APIRouter
from pydantic import BaseModel

from ..pnl.models import (
    PortfolioPnlSnapshot,
    PositionPnlSnapshot,
    aggregate_portfolio_pnl,
)


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


class UiPortfolioPnl(BaseModel):
    realized: Decimal
    unrealized: Decimal
    fees_paid: Decimal
    funding_paid: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal
    positions: Tuple[UiPositionPnl, ...]

    @classmethod
    def from_snapshot(cls, snapshot: PortfolioPnlSnapshot) -> "UiPortfolioPnl":
        positions = tuple(UiPositionPnl(**position.__dict__) for position in snapshot.positions)
        return cls(
            realized=snapshot.realized_pnl,
            unrealized=snapshot.unrealized_pnl,
            fees_paid=snapshot.fees_paid,
            funding_paid=snapshot.funding_paid,
            gross_pnl=snapshot.gross_pnl,
            net_pnl=snapshot.net_pnl,
            positions=positions,
        )


def _load_position_snapshots() -> Iterable[PositionPnlSnapshot]:
    """Placeholder data-source until real accounting is wired in."""

    return ()


@router.get("/pnl", response_model=UiPortfolioPnl)
def pnl() -> UiPortfolioPnl:
    snapshot = aggregate_portfolio_pnl(_load_position_snapshots())
    return UiPortfolioPnl.from_snapshot(snapshot)
