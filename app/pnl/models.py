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
from typing import Iterable, Tuple, cast

from ..utils.decimal import to_decimal
from ..strategies.registry import StrategyId


def _to_decimal(value: object) -> Decimal:
    return to_decimal(value, default=Decimal("0"))


@dataclass
class PositionPnlSnapshot:
    """Snapshot of PnL for an individual trading position."""

    symbol: str
    venue: str
    strategy_id: StrategyId | None = None
    size: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    mark_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    funding_paid: Decimal = Decimal("0")
    gross_pnl: Decimal | None = None
    net_pnl: Decimal | None = None
    notional_usd: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        self.symbol = str(self.symbol or "").upper()
        self.venue = str(self.venue or "").lower()
        self.strategy_id = str(self.strategy_id) if self.strategy_id else None
        self.size = _to_decimal(self.size)
        self.entry_price = _to_decimal(self.entry_price)
        self.mark_price = _to_decimal(self.mark_price)
        self.realized_pnl = _to_decimal(self.realized_pnl)
        self.unrealized_pnl = _to_decimal(self.unrealized_pnl)
        self.fees_paid = _to_decimal(self.fees_paid)
        self.funding_paid = _to_decimal(self.funding_paid)
        self.notional_usd = _to_decimal(self.notional_usd)

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

    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")
    funding_paid: Decimal = Decimal("0")
    gross_pnl: Decimal | None = None
    net_pnl: Decimal | None = None
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
    gross = sum((cast(Decimal, pos.gross_pnl) for pos in position_list), Decimal("0"))
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


@dataclass(slots=True)
class StrategyPnlSnapshot:
    strategy_id: StrategyId
    gross_pnl: Decimal
    net_pnl: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees_paid: Decimal
    funding_paid: Decimal
    positions_count: int
    notional_usd: Decimal


@dataclass(slots=True)
class StrategyExposureSnapshot:
    strategy_id: StrategyId
    notional_usd: Decimal
    net_qty: Decimal | None
    positions_count: int


@dataclass(slots=True)
class StrategyPerformanceSnapshot:
    strategy_id: StrategyId
    trades_count: int
    winning_trades: int
    losing_trades: int
    gross_pnl: Decimal
    net_pnl: Decimal
    average_trade_pnl: Decimal
    winrate: float
    turnover_notional: Decimal
    max_drawdown: Decimal | None


def build_strategy_pnl_snapshots(
    positions: Iterable[PositionPnlSnapshot],
) -> list[StrategyPnlSnapshot]:
    class _Accumulator:
        __slots__ = (
            "gross_pnl",
            "net_pnl",
            "realized_pnl",
            "unrealized_pnl",
            "fees_paid",
            "funding_paid",
            "positions_count",
            "notional_usd",
        )

        def __init__(self) -> None:
            self.gross_pnl = Decimal("0")
            self.net_pnl = Decimal("0")
            self.realized_pnl = Decimal("0")
            self.unrealized_pnl = Decimal("0")
            self.fees_paid = Decimal("0")
            self.funding_paid = Decimal("0")
            self.positions_count = 0
            self.notional_usd = Decimal("0")

    buckets: dict[StrategyId, _Accumulator] = {}

    for position in positions:
        strategy_id = position.strategy_id
        if not strategy_id:
            continue

        bucket = buckets.get(strategy_id)
        if bucket is None:
            bucket = _Accumulator()
            buckets[strategy_id] = bucket

        bucket.gross_pnl += position.gross_pnl
        bucket.net_pnl += position.net_pnl
        bucket.realized_pnl += position.realized_pnl
        bucket.unrealized_pnl += position.unrealized_pnl
        bucket.fees_paid += position.fees_paid
        bucket.funding_paid += position.funding_paid
        bucket.notional_usd += position.notional_usd.copy_abs()
        bucket.positions_count += 1

    snapshots: list[StrategyPnlSnapshot] = []
    for strategy_id, bucket in sorted(buckets.items()):
        snapshots.append(
            StrategyPnlSnapshot(
                strategy_id=strategy_id,
                gross_pnl=bucket.gross_pnl,
                net_pnl=bucket.net_pnl,
                realized_pnl=bucket.realized_pnl,
                unrealized_pnl=bucket.unrealized_pnl,
                fees_paid=bucket.fees_paid,
                funding_paid=bucket.funding_paid,
                positions_count=bucket.positions_count,
                notional_usd=bucket.notional_usd,
            )
        )

    return snapshots


__all__ = [
    "PositionPnlSnapshot",
    "PortfolioPnlSnapshot",
    "StrategyPnlSnapshot",
    "StrategyPerformanceSnapshot",
    "StrategyExposureSnapshot",
    "aggregate_portfolio_pnl",
    "build_strategy_pnl_snapshots",
]
