"""Domain models for portfolio and position PnL snapshots.

The goal of these models is to provide a single, structured place where
realized/unrealized profit and loss as well as fees and funding are tracked.
This file intentionally keeps the logic very lightweight so that more
advanced accounting (introduced in follow-up work) can plug into the same
abstractions without having to change the public API surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Tuple

from ..utils.decimal import to_decimal


def _to_decimal(value: object) -> Decimal:
    return to_decimal(value, default=Decimal("0"))


@dataclass
class PositionPnlSnapshot:
    """Snapshot of PnL for an individual trading position."""

    symbol: str
    venue: str
    size: object = Decimal("0")
    entry_price: object = Decimal("0")
    mark_price: object = Decimal("0")
    realized_pnl: object = Decimal("0")
    unrealized_pnl: object = Decimal("0")
    fees_paid: object = Decimal("0")
    funding_paid: object = Decimal("0")
    gross_pnl: object | None = None
    net_pnl: object | None = None

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol or "").upper()
        self.venue = str(self.venue or "").lower()
        self.size = _to_decimal(self.size)
        self.entry_price = _to_decimal(self.entry_price)
        self.mark_price = _to_decimal(self.mark_price)
        self.realized_pnl = _to_decimal(self.realized_pnl)
        self.unrealized_pnl = _to_decimal(self.unrealized_pnl)
        self.fees_paid = _to_decimal(self.fees_paid)
        self.funding_paid = _to_decimal(self.funding_paid)

        gross = self.gross_pnl
        if gross is None:
            gross = self.realized_pnl + self.unrealized_pnl
        self.gross_pnl = _to_decimal(gross)

        net = self.net_pnl
        if net is None:
            net = self.gross_pnl + self.fees_paid + self.funding_paid
        self.net_pnl = _to_decimal(net)


@dataclass
class PortfolioPnlSnapshot:
    """Aggregated PnL snapshot across multiple positions."""

    realized_pnl: object = Decimal("0")
    unrealized_pnl: object = Decimal("0")
    fees_paid: object = Decimal("0")
    funding_paid: object = Decimal("0")
    gross_pnl: object | None = None
    net_pnl: object | None = None
    positions: Tuple[PositionPnlSnapshot, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        self.realized_pnl = _to_decimal(self.realized_pnl)
        self.unrealized_pnl = _to_decimal(self.unrealized_pnl)
        self.fees_paid = _to_decimal(self.fees_paid)
        self.funding_paid = _to_decimal(self.funding_paid)

        gross = self.gross_pnl
        if gross is None:
            gross = self.realized_pnl + self.unrealized_pnl
        self.gross_pnl = _to_decimal(gross)

        net = self.net_pnl
        if net is None:
            net = self.gross_pnl + self.fees_paid + self.funding_paid
        self.net_pnl = _to_decimal(net)

        if not isinstance(self.positions, tuple):
            self.positions = tuple(self.positions)


def aggregate_portfolio_pnl(
    positions: Iterable[PositionPnlSnapshot],
) -> PortfolioPnlSnapshot:
    """Aggregate per-position snapshots into a portfolio snapshot."""

    position_list = tuple(positions)
    realized = sum((pos.realized_pnl for pos in position_list), Decimal("0"))
    unrealized = sum((pos.unrealized_pnl for pos in position_list), Decimal("0"))
    fees = sum((pos.fees_paid for pos in position_list), Decimal("0"))
    funding = sum((pos.funding_paid for pos in position_list), Decimal("0"))
    gross = sum((pos.gross_pnl for pos in position_list), Decimal("0"))
    net = gross + fees + funding

    return PortfolioPnlSnapshot(
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        fees_paid=fees,
        funding_paid=funding,
        gross_pnl=gross,
        net_pnl=net,
        positions=position_list,
    )


__all__ = [
    "PositionPnlSnapshot",
    "PortfolioPnlSnapshot",
    "aggregate_portfolio_pnl",
]
