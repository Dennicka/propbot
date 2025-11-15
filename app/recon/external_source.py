"""Stub loader for external exchange state used by reconciliation."""

from __future__ import annotations

from typing import Sequence

from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
    VenueId,
)


class ExternalStateSource:
    """Facade to load external balances/positions/orders from exchanges for reconciliation."""

    async def load_balances(self, venue_id: VenueId) -> Sequence[ExchangeBalanceSnapshot]:
        # NOTE: stub for now, will be implemented with real exchange adapters.
        return []

    async def load_positions(self, venue_id: VenueId) -> Sequence[ExchangePositionSnapshot]:
        # NOTE: stub for now.
        return []

    async def load_open_orders(self, venue_id: VenueId) -> Sequence[ExchangeOrderSnapshot]:
        # NOTE: stub for now.
        return []


__all__ = ["ExternalStateSource"]
