"""Protocol definitions for futures exchange clients."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class FuturesExchangeClient(Protocol):
    """Interface for futures exchange clients used for hedged execution."""

    def get_best_bid_ask(self, symbol: str) -> dict:
        """Return the best bid/ask for the provided symbol."""

    def open_long(self, symbol: str, qty_usdt: float, leverage: float) -> dict:
        """Open a long position using the provided notional size and leverage."""

    def open_short(self, symbol: str, qty_usdt: float, leverage: float) -> dict:
        """Open a short position using the provided notional size and leverage."""

    def close_position(self, symbol: str) -> dict:
        """Close any open position for the given symbol."""
