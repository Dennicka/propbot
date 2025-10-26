"""Binance USDⓈ-M perpetual futures client."""

from __future__ import annotations

import os
from typing import Any, Dict

from .base import FuturesExchangeClient


class BinanceFuturesClient(FuturesExchangeClient):
    """Minimal Binance futures client used by the cross-exchange arbitrage service."""

    api_key: str | None
    api_secret: str | None
    api_url: str

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_url: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("BINANCE_API_KEY")
        self.api_secret = api_secret or os.getenv("BINANCE_API_SECRET")
        self.api_url = api_url or os.getenv(
            "BINANCE_FUTURES_API_URL", "https://fapi.binance.com"
        )

    def get_best_bid_ask(self, symbol: str) -> Dict[str, Any]:
        """Return the best bid/ask from Binance Futures.

        TODO: Replace the stub implementation with a real REST or WebSocket call.
        """

        # Placeholder implementation to keep the interface functional for tests.
        return {"symbol": symbol, "bid": 0.0, "ask": 0.0}

    def open_long(self, symbol: str, qty_usdt: float, leverage: float) -> Dict[str, Any]:
        """Open a long position on Binance Futures.

        TODO: Implement REST call to create a long position using leverage.
        """

        return {
            "exchange": "binance",
            "symbol": symbol,
            "side": "long",
            "notional_usdt": qty_usdt,
            "leverage": leverage,
            "status": "stub",
        }

    def open_short(self, symbol: str, qty_usdt: float, leverage: float) -> Dict[str, Any]:
        """Open a short position on Binance Futures.

        TODO: Implement REST call to create a short position using leverage.
        """

        return {
            "exchange": "binance",
            "symbol": symbol,
            "side": "short",
            "notional_usdt": qty_usdt,
            "leverage": leverage,
            "status": "stub",
        }

    def close_position(self, symbol: str) -> Dict[str, Any]:
        """Close a position on Binance Futures.

        TODO: Implement REST call to close the open position.
        """

        return {"exchange": "binance", "symbol": symbol, "status": "stub"}
