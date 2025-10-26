"""OKX USDT-margined perpetual futures client."""

from __future__ import annotations

import os
from typing import Any, Dict

from .base import FuturesExchangeClient


class OKXFuturesClient(FuturesExchangeClient):
    """Minimal OKX futures client used by the cross-exchange arbitrage service."""

    api_key: str | None
    api_secret: str | None
    passphrase: str | None
    api_url: str

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        api_url: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OKX_API_KEY")
        self.api_secret = api_secret or os.getenv("OKX_API_SECRET")
        self.passphrase = passphrase or os.getenv("OKX_API_PASSPHRASE")
        self.api_url = api_url or os.getenv("OKX_FUTURES_API_URL", "https://www.okx.com")

    def get_best_bid_ask(self, symbol: str) -> Dict[str, Any]:
        """Return the best bid/ask from OKX futures orderbook.

        TODO: Replace the stub implementation with a real REST or WebSocket call.
        """

        return {"symbol": symbol, "bid": 0.0, "ask": 0.0}

    def open_long(self, symbol: str, qty_usdt: float, leverage: float) -> Dict[str, Any]:
        """Open a long position on OKX perpetual futures."""

        return {
            "exchange": "okx",
            "symbol": symbol,
            "side": "long",
            "notional_usdt": qty_usdt,
            "leverage": leverage,
            "status": "stub",
        }

    def open_short(self, symbol: str, qty_usdt: float, leverage: float) -> Dict[str, Any]:
        """Open a short position on OKX perpetual futures."""

        return {
            "exchange": "okx",
            "symbol": symbol,
            "side": "short",
            "notional_usdt": qty_usdt,
            "leverage": leverage,
            "status": "stub",
        }

    def close_position(self, symbol: str) -> Dict[str, Any]:
        """Close an OKX perpetual futures position."""

        return {"exchange": "okx", "symbol": symbol, "status": "stub"}
