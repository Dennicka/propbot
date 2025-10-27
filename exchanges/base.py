"""Protocol definitions for futures exchange clients."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class FuturesExchangeClient(Protocol):
    """Interface for futures exchange clients used for hedged execution."""

    def get_mark_price(self, symbol: str) -> dict:
        """Return the mark price payload for *symbol*."""

    def get_position(self, symbol: str) -> dict:
        """Return current position information for *symbol*."""

    def place_order(self, symbol: str, side: str, notional_usdt: float, leverage: float) -> dict:
        """Submit a market order using *notional_usdt* notion and *leverage*."""

    def cancel_all(self, symbol: str) -> dict:
        """Cancel all open orders for *symbol*."""

    def get_account_limits(self) -> dict:
        """Return available balance or margin information."""
