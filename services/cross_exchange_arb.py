"""Cross-exchange arbitrage helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from app.services.runtime import HoldActiveError, is_dry_run_mode, register_order_attempt
from exchanges import BinanceFuturesClient, OKXFuturesClient


@dataclass
class _ExchangeClients:
    binance: BinanceFuturesClient
    okx: OKXFuturesClient


_clients = _ExchangeClients(
    binance=BinanceFuturesClient(),
    okx=OKXFuturesClient(),
)


def _determine_cheapest(
    binance_quote: Dict[str, float], okx_quote: Dict[str, float]
) -> Tuple[str, float]:
    if binance_quote["ask"] <= okx_quote["ask"]:
        return "binance", float(binance_quote["ask"])
    return "okx", float(okx_quote["ask"])


def _determine_most_expensive(
    binance_quote: Dict[str, float], okx_quote: Dict[str, float]
) -> Tuple[str, float]:
    if binance_quote["bid"] >= okx_quote["bid"]:
        return "binance", float(binance_quote["bid"])
    return "okx", float(okx_quote["bid"])


def check_spread(symbol: str) -> dict:
    """Inspect quotes from both exchanges and compute the actionable spread."""

    binance_quote = _clients.binance.get_best_bid_ask(symbol)
    okx_quote = _clients.okx.get_best_bid_ask(symbol)

    cheap_exchange, cheap_ask = _determine_cheapest(binance_quote, okx_quote)
    expensive_exchange, expensive_bid = _determine_most_expensive(
        binance_quote, okx_quote
    )

    spread = float(expensive_bid) - float(cheap_ask)
    spread_bps = (spread / float(cheap_ask)) * 10_000 if cheap_ask else 0.0

    return {
        "symbol": symbol,
        "cheap": cheap_exchange,
        "expensive": expensive_exchange,
        "cheap_ask": float(cheap_ask),
        "expensive_bid": float(expensive_bid),
        "spread": spread,
        "spread_bps": float(spread_bps),
    }


def execute_hedged_trade(
    symbol: str, notion_usdt: float, leverage: float, min_spread: float
) -> dict:
    """Open a hedged position across exchanges when spread exceeds threshold."""

    spread_info = check_spread(symbol)
    spread_value = float(spread_info["spread"])

    if spread_value < float(min_spread):
        return {
            "symbol": symbol,
            "min_spread": float(min_spread),
            "spread": spread_value,
            "success": False,
            "reason": "spread_below_threshold",
            "details": spread_info,
        }

    cheap_exchange = spread_info["cheap"]
    expensive_exchange = spread_info["expensive"]

    if cheap_exchange == "binance":
        long_client = _clients.binance
        short_client = _clients.okx
    else:
        long_client = _clients.okx
        short_client = _clients.binance

    dry_run_mode = is_dry_run_mode()

    def _simulated_order(exchange: str, side: str) -> Dict[str, object]:
        return {
            "exchange": exchange,
            "symbol": symbol,
            "side": side,
            "notional_usdt": notion_usdt,
            "leverage": leverage,
            "status": "simulated",
            "simulated": True,
        }

    try:
        register_order_attempt(reason="runaway_orders_per_min", source="cross_exchange_long")
        if dry_run_mode:
            long_order = _simulated_order(cheap_exchange, "long")
        else:
            long_order = long_client.open_long(symbol, notion_usdt, leverage)
        register_order_attempt(reason="runaway_orders_per_min", source="cross_exchange_short")
        if dry_run_mode:
            short_order = _simulated_order(expensive_exchange, "short")
        else:
            short_order = short_client.open_short(symbol, notion_usdt, leverage)
    except HoldActiveError as exc:
        return {
            "symbol": symbol,
            "min_spread": float(min_spread),
            "spread": spread_value,
            "success": False,
            "reason": exc.reason,
            "details": spread_info,
            "hold_active": True,
        }
    long_order.setdefault("price", float(spread_info["cheap_ask"]))
    short_order.setdefault("price", float(spread_info["expensive_bid"]))

    return {
        "symbol": symbol,
        "min_spread": float(min_spread),
        "spread": spread_value,
        "spread_bps": float(spread_info.get("spread_bps", 0.0)),
        "cheap_exchange": cheap_exchange,
        "expensive_exchange": expensive_exchange,
        "long_order": long_order,
        "short_order": short_order,
        "success": True,
        "status": "simulated" if dry_run_mode else "executed",
        "dry_run_mode": dry_run_mode,
        "simulated": dry_run_mode,
        "details": spread_info,
    }
