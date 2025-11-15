"""Loader for external exchange state used by reconciliation."""

from __future__ import annotations

from typing import Mapping, Sequence

from app.recon.external_client import ExchangeAccountClient
from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
    VenueId,
)


class ExternalStateSource:
    """Facade to load external balances/positions/orders from exchanges for reconciliation."""

    def __init__(
        self,
        clients: Mapping[VenueId, ExchangeAccountClient] | None = None,
    ) -> None:
        self._clients: dict[VenueId, ExchangeAccountClient] = dict(clients or {})

    def _get_client(self, venue_id: VenueId) -> ExchangeAccountClient | None:
        client = self._clients.get(venue_id)
        if client is not None:
            return client
        from app.recon.external_factories import get_exchange_account_client_for_venue

        client = get_exchange_account_client_for_venue(venue_id)
        if client is None:
            return None
        self._clients[venue_id] = client
        return client

    async def load_balances(self, venue_id: VenueId) -> Sequence[ExchangeBalanceSnapshot]:
        client = self._get_client(venue_id)
        if client is None:
            return []
        return await client.load_balances(venue_id)

    async def load_positions(self, venue_id: VenueId) -> Sequence[ExchangePositionSnapshot]:
        client = self._get_client(venue_id)
        if client is None:
            return []
        return await client.load_positions(venue_id)

    async def load_open_orders(self, venue_id: VenueId) -> Sequence[ExchangeOrderSnapshot]:
        client = self._get_client(venue_id)
        if client is None:
            return []
        return await client.load_open_orders(venue_id)


__all__ = ["ExternalStateSource"]
