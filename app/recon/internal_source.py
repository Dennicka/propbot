"""Facade for loading internal state snapshots for reconciliation."""

from __future__ import annotations

from typing import Sequence

from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
    VenueId,
)


class InternalStateSource:
    """Facade to load internal balances/positions/orders for reconciliation."""

    async def load_balances(self, venue_id: VenueId) -> Sequence[ExchangeBalanceSnapshot]:
        # NOTE: placeholder implementation; integrate with ledger/persistence layer.
        return []

    async def load_positions(self, venue_id: VenueId) -> Sequence[ExchangePositionSnapshot]:
        # NOTE: placeholder implementation; integrate with portfolio/positions repositories.
        return []

    async def load_open_orders(self, venue_id: VenueId) -> Sequence[ExchangeOrderSnapshot]:
        # NOTE: placeholder implementation; integrate with order journal/execution state.
        return []


__all__ = ["InternalStateSource"]
