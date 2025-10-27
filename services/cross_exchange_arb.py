"""Cross-exchange arbitrage helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Mapping, Tuple

from app.services.hedge_log import append_entry
from app.services.runtime import (
    HoldActiveError,
    engage_safety_hold,
    is_dry_run_mode,
    register_order_attempt,
)
from positions import create_position
from exchanges import BinanceFuturesClient, OKXFuturesClient


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _ExchangeClients:
    binance: BinanceFuturesClient
    okx: OKXFuturesClient


_clients = _ExchangeClients(
    binance=BinanceFuturesClient(),
    okx=OKXFuturesClient(),
)


def _determine_cheapest(
    binance_mark: Dict[str, float], okx_mark: Dict[str, float]
) -> Tuple[str, float]:
    if binance_mark["mark_price"] <= okx_mark["mark_price"]:
        return "binance", float(binance_mark["mark_price"])
    return "okx", float(okx_mark["mark_price"])


def _determine_most_expensive(
    binance_mark: Dict[str, float], okx_mark: Dict[str, float]
) -> Tuple[str, float]:
    if binance_mark["mark_price"] >= okx_mark["mark_price"]:
        return "binance", float(binance_mark["mark_price"])
    return "okx", float(okx_mark["mark_price"])


def _client_for(venue: str) -> BinanceFuturesClient | OKXFuturesClient:
    if venue == "binance":
        return _clients.binance
    if venue == "okx":
        return _clients.okx
    raise ValueError(f"unknown exchange venue: {venue}")


def _build_leg(
    *,
    exchange: str,
    symbol: str,
    side: str,
    notional_usdt: float,
    leverage: float,
    price: float,
    filled_qty: float,
    status: str,
    simulated: bool,
    raw: Dict[str, object] | None = None,
) -> Dict[str, object]:
    base_size = float(filled_qty)
    entry = {
        "exchange": exchange,
        "symbol": symbol,
        "side": side,
        "status": status,
        "avg_price": float(price),
        "price": float(price),
        "filled_qty": base_size,
        "base_size": base_size,
        "notional_usdt": float(notional_usdt),
        "leverage": float(leverage),
        "simulated": simulated,
    }
    if raw:
        if raw.get("order_id"):
            entry["order_id"] = raw.get("order_id")
        entry["raw"] = raw
    return entry


def _simulate_leg(
    *,
    exchange: str,
    symbol: str,
    side: str,
    notional_usdt: float,
    leverage: float,
    price: float,
) -> Dict[str, object]:
    price_value = float(price) if price else 0.0
    if price_value <= 0:
        filled_qty = 0.0
    else:
        filled_qty = float(notional_usdt) / price_value
    return _build_leg(
        exchange=exchange,
        symbol=symbol,
        side=side,
        notional_usdt=notional_usdt,
        leverage=leverage,
        price=price_value,
        filled_qty=filled_qty,
        status="simulated",
        simulated=True,
    )


def _normalise_order(
    *,
    order: Dict[str, object],
    exchange: str,
    symbol: str,
    side: str,
    notional_usdt: float,
    leverage: float,
    fallback_price: float,
) -> Dict[str, object]:
    price = float(order.get("avg_price") or order.get("price") or fallback_price or 0.0)
    filled_qty = float(order.get("filled_qty") or order.get("base_size") or 0.0)
    if not filled_qty and price > 0:
        filled_qty = float(notional_usdt) / float(price)
    status = str(order.get("status") or "filled").lower()
    payload = dict(order)
    payload.setdefault("order_id", order.get("order_id"))
    return _build_leg(
        exchange=exchange,
        symbol=symbol,
        side=side,
        notional_usdt=notional_usdt,
        leverage=leverage,
        price=price,
        filled_qty=filled_qty,
        status=status,
        simulated=False,
        raw=payload,
    )


def check_spread(symbol: str) -> dict:
    """Inspect quotes from both exchanges and compute the actionable spread."""

    symbol_upper = str(symbol).upper()
    binance_mark = _clients.binance.get_mark_price(symbol_upper)
    okx_mark = _clients.okx.get_mark_price(symbol_upper)

    binance_price = float(binance_mark.get("mark_price") or 0.0)
    okx_price = float(okx_mark.get("mark_price") or 0.0)

    cheap_exchange, cheap_price = _determine_cheapest(
        {"mark_price": binance_price}, {"mark_price": okx_price}
    )
    expensive_exchange, expensive_price = _determine_most_expensive(
        {"mark_price": binance_price}, {"mark_price": okx_price}
    )

    spread = float(expensive_price) - float(cheap_price)
    spread_bps = (spread / float(cheap_price)) * 10_000 if cheap_price else 0.0

    return {
        "symbol": symbol_upper,
        "cheap": cheap_exchange,
        "expensive": expensive_exchange,
        "cheap_mark": float(cheap_price),
        "expensive_mark": float(expensive_price),
        "cheap_ask": float(cheap_price),
        "expensive_bid": float(expensive_price),
        "binance_mark_price": binance_price,
        "okx_mark_price": okx_price,
        "spread": spread,
        "spread_bps": float(spread_bps),
    }


def _log_partial_failure(
    *,
    symbol: str,
    notional_usdt: float,
    leverage: float,
    cheap_exchange: str,
    expensive_exchange: str,
    reason: str,
    long_leg: Dict[str, object] | None,
    short_leg: Dict[str, object] | None,
) -> None:
    append_entry(
        {
            "timestamp": _ts(),
            "symbol": symbol,
            "long_venue": cheap_exchange,
            "short_venue": expensive_exchange,
            "notional_usdt": float(notional_usdt),
            "leverage": float(leverage),
            "result": f"partial_failure:{reason}",
            "status": "partial_failure",
            "simulated": False,
            "dry_run_mode": False,
            "legs": [leg for leg in (long_leg, short_leg) if leg],
            "error": reason,
            "initiator": "cross_exchange_execute",
        }
    )


def _persist_partial_position(
    *,
    symbol: str,
    notional_usdt: float,
    leverage: float,
    cheap_exchange: str,
    expensive_exchange: str,
    spread_info: Mapping[str, object] | None,
    long_leg: Dict[str, object] | None,
    short_leg: Dict[str, object] | None,
) -> Dict[str, object] | None:
    """Persist a partially hedged position to the durable store."""

    if long_leg is None and short_leg is None:
        return None

    timestamp = _ts()

    def _leg_payload(
        leg: Dict[str, object] | None,
        *,
        venue: str,
        symbol: str,
        side: str,
        status: str,
    ) -> Dict[str, object]:
        if leg:
            entry_price = float(
                leg.get("avg_price")
                or leg.get("price")
                or leg.get("entry_price")
                or 0.0
            )
            base_size = float(leg.get("base_size") or leg.get("filled_qty") or 0.0)
            notional_value = float(leg.get("notional_usdt") or notional_usdt)
            if not base_size and entry_price:
                try:
                    base_size = notional_value / entry_price
                except ZeroDivisionError:
                    base_size = 0.0
            payload = {
                "venue": str(leg.get("exchange") or venue),
                "symbol": str(leg.get("symbol") or symbol),
                "side": str(leg.get("side") or side),
                "notional_usdt": float(notional_value),
                "entry_price": entry_price or None,
                "base_size": base_size,
                "timestamp": str(leg.get("timestamp") or timestamp),
                "status": status,
            }
            return payload
        return {
            "venue": str(venue),
            "symbol": str(symbol),
            "side": str(side),
            "notional_usdt": 0.0,
            "entry_price": None,
            "base_size": 0.0,
            "timestamp": timestamp,
            "status": "missing",
        }

    try:
        entry_long_price = None
        entry_short_price = None
        if long_leg:
            entry_long_price = float(
                long_leg.get("avg_price")
                or long_leg.get("price")
                or long_leg.get("entry_price")
                or 0.0
            ) or None
        if short_leg:
            entry_short_price = float(
                short_leg.get("avg_price")
                or short_leg.get("price")
                or short_leg.get("entry_price")
                or 0.0
            ) or None
        legs = [
            _leg_payload(
                long_leg,
                venue=cheap_exchange,
                symbol=symbol,
                side="long",
                status="partial" if long_leg else "missing",
            ),
            _leg_payload(
                short_leg,
                venue=expensive_exchange,
                symbol=symbol,
                side="short",
                status="partial" if short_leg else "missing",
            ),
        ]
        spread_bps = 0.0
        if isinstance(spread_info, Mapping):
            try:
                spread_bps = float(spread_info.get("spread_bps") or 0.0)
            except (TypeError, ValueError):
                spread_bps = 0.0
        record = create_position(
            symbol=symbol,
            long_venue=cheap_exchange,
            short_venue=expensive_exchange,
            notional_usdt=float(notional_usdt),
            entry_spread_bps=spread_bps,
            leverage=float(leverage),
            entry_long_price=entry_long_price,
            entry_short_price=entry_short_price,
            status="partial",
            simulated=False,
            legs=legs,
        )
        return record
    except Exception:  # pragma: no cover - defensive persistence
        return None


def execute_hedged_trade(
    symbol: str, notion_usdt: float, leverage: float, min_spread: float
) -> dict:
    """Open a hedged position across exchanges when spread exceeds threshold."""

    spread_info = check_spread(symbol)
    spread_value = float(spread_info["spread"])

    if spread_value < float(min_spread):
        return {
            "symbol": spread_info["symbol"],
            "min_spread": float(min_spread),
            "spread": spread_value,
            "success": False,
            "reason": "spread_below_threshold",
            "details": spread_info,
        }

    cheap_exchange = spread_info["cheap"]
    expensive_exchange = spread_info["expensive"]

    long_client = _client_for(cheap_exchange)
    short_client = _client_for(expensive_exchange)

    dry_run_mode = is_dry_run_mode()
    notional = float(notion_usdt)
    leverage_value = float(leverage)

    long_leg = None
    short_leg = None

    try:
        register_order_attempt(reason="runaway_orders_per_min", source="cross_exchange_long")
        if dry_run_mode:
            long_leg = _simulate_leg(
                exchange=cheap_exchange,
                symbol=spread_info["symbol"],
                side="long",
                notional_usdt=notional,
                leverage=leverage_value,
                price=float(spread_info["cheap_mark"]),
            )
        else:
            long_order = long_client.place_order(
                spread_info["symbol"],
                "long",
                notional,
                leverage_value,
            )
            long_leg = _normalise_order(
                order=long_order,
                exchange=cheap_exchange,
                symbol=spread_info["symbol"],
                side="long",
                notional_usdt=notional,
                leverage=leverage_value,
                fallback_price=float(spread_info["cheap_mark"]),
            )

        register_order_attempt(reason="runaway_orders_per_min", source="cross_exchange_short")
        if dry_run_mode:
            short_leg = _simulate_leg(
                exchange=expensive_exchange,
                symbol=spread_info["symbol"],
                side="short",
                notional_usdt=notional,
                leverage=leverage_value,
                price=float(spread_info["expensive_mark"]),
            )
        else:
            short_order = short_client.place_order(
                spread_info["symbol"],
                "short",
                notional,
                leverage_value,
            )
            short_leg = _normalise_order(
                order=short_order,
                exchange=expensive_exchange,
                symbol=spread_info["symbol"],
                side="short",
                notional_usdt=notional,
                leverage=leverage_value,
                fallback_price=float(spread_info["expensive_mark"]),
            )
    except HoldActiveError as exc:
        partial_record = None
        if not dry_run_mode and ((long_leg and not short_leg) or (short_leg and not long_leg)):
            partial_record = _persist_partial_position(
                symbol=spread_info["symbol"],
                notional_usdt=notional,
                leverage=leverage_value,
                cheap_exchange=cheap_exchange,
                expensive_exchange=expensive_exchange,
                spread_info=spread_info,
                long_leg=long_leg,
                short_leg=short_leg,
            )
        return {
            "symbol": spread_info["symbol"],
            "min_spread": float(min_spread),
            "spread": spread_value,
            "success": False,
            "reason": exc.reason,
            "details": spread_info,
            "hold_active": True,
            "partial_position": partial_record,
        }
    except Exception as exc:
        reason = str(exc)
        partial = bool(long_leg and not short_leg and not dry_run_mode)
        if partial:
            _log_partial_failure(
                symbol=spread_info["symbol"],
                notional_usdt=notional,
                leverage=leverage_value,
                cheap_exchange=cheap_exchange,
                expensive_exchange=expensive_exchange,
                reason=reason,
                long_leg=long_leg,
                short_leg=short_leg,
            )
            engage_safety_hold("hedge_leg_failed", source="cross_exchange_hedge")
            _persist_partial_position(
                symbol=spread_info["symbol"],
                notional_usdt=notional,
                leverage=leverage_value,
                cheap_exchange=cheap_exchange,
                expensive_exchange=expensive_exchange,
                spread_info=spread_info,
                long_leg=long_leg,
                short_leg=short_leg,
            )
        return {
            "symbol": spread_info["symbol"],
            "min_spread": float(min_spread),
            "spread": spread_value,
            "success": False,
            "reason": "short_leg_failed" if partial else "order_failed",
            "details": spread_info,
            "long_leg": long_leg,
            "short_leg": short_leg,
            "error": reason,
            "hold_engaged": partial,
        }

    legs = [leg for leg in (long_leg, short_leg) if leg]
    result = {
        "symbol": spread_info["symbol"],
        "min_spread": float(min_spread),
        "spread": spread_value,
        "spread_bps": float(spread_info.get("spread_bps", 0.0)),
        "cheap_exchange": cheap_exchange,
        "expensive_exchange": expensive_exchange,
        "legs": legs,
        "long_order": long_leg,
        "short_order": short_leg,
        "success": True,
        "status": "simulated" if dry_run_mode else "executed",
        "dry_run_mode": dry_run_mode,
        "simulated": dry_run_mode,
        "details": spread_info,
    }
    return result
