from __future__ import annotations

from typing import Protocol, Sequence

from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
    VenueId,
)


class ExchangeAccountClient(Protocol):
    """Minimal read-only interface for recon to query external exchange state."""

    async def load_balances(self, venue_id: VenueId) -> Sequence[ExchangeBalanceSnapshot]: ...

    async def load_positions(self, venue_id: VenueId) -> Sequence[ExchangePositionSnapshot]: ...

    async def load_open_orders(self, venue_id: VenueId) -> Sequence[ExchangeOrderSnapshot]: ...


__all__ = ["ExchangeAccountClient"]
