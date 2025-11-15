"""Datamodels describing reconciliation snapshots and issues."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Sequence

VenueId = str
Symbol = str

ReconIssueSeverity = Literal["info", "warning", "error"]
ReconIssueKind = Literal[
    "balance_mismatch",
    "position_mismatch",
    "order_mismatch",
    "missing_internal",
    "missing_external",
    "notional_gap",
]


@dataclass(slots=True)
class ExchangeBalanceSnapshot:
    """Snapshot of balances on a single venue (external or internal view)."""

    venue_id: VenueId
    asset: str
    total: Decimal
    available: Decimal


@dataclass(slots=True)
class ExchangePositionSnapshot:
    """Snapshot of positions on a single venue (external or internal view)."""

    venue_id: VenueId
    symbol: Symbol
    qty: Decimal
    entry_price: Decimal | None
    notional: Decimal


@dataclass(slots=True)
class ExchangeOrderSnapshot:
    """Snapshot of active orders on a single venue."""

    venue_id: VenueId
    symbol: Symbol
    client_order_id: str | None
    exchange_order_id: str | None
    side: Literal["buy", "sell"]
    qty: Decimal
    price: Decimal
    status: str  # e.g. "open", "partially_filled", "pending_cancel"


@dataclass(slots=True)
class ReconIssue:
    """Single reconciliation issue between internal and external views."""

    severity: ReconIssueSeverity
    kind: ReconIssueKind
    venue_id: VenueId
    symbol: Symbol | None
    asset: str | None
    message: str
    internal_value: str | None = None
    external_value: str | None = None


@dataclass(slots=True)
class ReconSnapshot:
    """Aggregated reconciliation result for a single venue."""

    venue_id: VenueId
    balances_internal: Sequence[ExchangeBalanceSnapshot]
    balances_external: Sequence[ExchangeBalanceSnapshot]
    positions_internal: Sequence[ExchangePositionSnapshot]
    positions_external: Sequence[ExchangePositionSnapshot]
    open_orders_internal: Sequence[ExchangeOrderSnapshot]
    open_orders_external: Sequence[ExchangeOrderSnapshot]

    issues: Sequence[ReconIssue]


__all__ = [
    "VenueId",
    "Symbol",
    "ReconIssueSeverity",
    "ReconIssueKind",
    "ExchangeBalanceSnapshot",
    "ExchangePositionSnapshot",
    "ExchangeOrderSnapshot",
    "ReconIssue",
    "ReconSnapshot",
]
