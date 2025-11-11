"""Cross-exchange arbitrage helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Tuple

from app.services.hedge_log import append_entry
from app.services.runtime import (
    HoldActiveError,
    engage_safety_hold,
    is_dry_run_mode,
    record_incident,
    register_order_attempt,
)
from app.metrics import slo
from app.risk.telemetry import record_risk_skip
from app.strategy_budget import get_strategy_budget_manager
from app.strategy_risk import get_strategy_risk_manager
from positions import create_position
from exchanges import BinanceFuturesClient, OKXFuturesClient
from .execution_router import choose_venue
from .execution_stats_store import append_entry as store_execution_stat
from .edge_guard import allowed_to_trade as guard_allowed_to_trade


LOGGER = logging.getLogger(__name__)


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


STRATEGY_NAME = "cross_exchange_arb"


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


def _leg_successful(leg: Dict[str, Any] | None) -> bool:
    if not leg:
        return False
    status = str(leg.get("status") or "").lower()
    return status in {"filled", "executed", "success", "simulated"}


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_leg_price(leg: Dict[str, Any] | None) -> float:
    if not leg:
        return 0.0
    for key in ("avg_price", "price", "entry_price"):
        if key in leg:
            value = leg.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
    return 0.0


def _record_execution_stat(
    *,
    symbol: str,
    side: str,
    plan: Mapping[str, Any] | None,
    leg: Dict[str, Any] | None,
    success: bool,
    dry_run: bool,
    failure_reason: str | None = None,
) -> None:
    if not isinstance(plan, Mapping):
        return
    try:
        planned_px = _coerce_float(plan.get("expected_fill_px"))
        planned_size = _coerce_float(plan.get("size"))
        expected_notional = _coerce_float(plan.get("expected_notional"))
        if planned_size <= 0.0 and planned_px > 0.0 and expected_notional > 0.0:
            planned_size = expected_notional / planned_px
    except Exception:  # pragma: no cover - defensive
        planned_px = 0.0
        planned_size = 0.0
        expected_notional = 0.0
    executed_px = _extract_leg_price(leg)
    actual_size = planned_size
    if leg:
        filled_qty = _coerce_float(leg.get("filled_qty"))
        base_size = _coerce_float(leg.get("base_size"))
        actual_size = filled_qty or base_size or actual_size
    slippage_bps = None
    if planned_px > 0.0 and executed_px > 0.0:
        side_lower = str(side or "").lower()
        if side_lower in {"buy", "long"}:
            delta = executed_px - planned_px
        else:
            delta = planned_px - executed_px
        slippage_bps = (delta / planned_px) * 10_000.0
    record = {
        "timestamp": _ts(),
        "symbol": str(symbol).upper(),
        "venue": str(plan.get("venue") or ""),
        "side": str(side or "").lower(),
        "planned_px": planned_px or None,
        "real_fill_px": executed_px or None,
        "size": actual_size or 0.0,
        "success": bool(success),
        "dry_run": bool(dry_run),
        "slippage_bps": slippage_bps,
        "failure_reason": failure_reason,
    }
    try:
        store_execution_stat(record)
    except Exception as exc:  # pragma: no cover - store must not break hedge flow  # noqa: BLE001
        LOGGER.warning(
            "cross_exchange_arb execution stat persistence failed",
            extra={"symbol": symbol, "side": side},
            exc_info=exc,
        )


def _log_edge_guard_block(
    *,
    symbol: str,
    reason: str,
    spread_info: Mapping[str, Any],
    long_plan: Mapping[str, Any] | None,
    short_plan: Mapping[str, Any] | None,
) -> None:
    dry_run = is_dry_run_mode()
    timestamp = _ts()
    entry = {
        "timestamp": timestamp,
        "symbol": symbol,
        "result": "edge_guard_blocked",
        "status": "rejected",
        "reason": reason,
        "simulated": dry_run,
        "dry_run_mode": dry_run,
        "initiator": "cross_exchange_execute",
        "details": {
            "spread": float(spread_info.get("spread", 0.0)),
            "spread_bps": float(spread_info.get("spread_bps", 0.0)),
            "long_plan": dict(long_plan or {}),
            "short_plan": dict(short_plan or {}),
        },
    }
    try:
        append_entry(entry)
    except Exception as exc:  # pragma: no cover - logging best effort  # noqa: BLE001
        LOGGER.warning(
            "cross_exchange_arb hedge log append failed",
            extra={"symbol": symbol, "reason": reason},
            exc_info=exc,
        )
    try:
        record_incident(
            "edge_guard_blocked",
            {
                "symbol": symbol,
                "reason": reason,
                "spread_bps": float(spread_info.get("spread_bps", 0.0)),
            },
        )
    except Exception as exc:  # pragma: no cover - incident log best effort  # noqa: BLE001
        LOGGER.warning(
            "cross_exchange_arb incident record failed",
            extra={"symbol": symbol, "reason": reason},
            exc_info=exc,
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
                leg.get("avg_price") or leg.get("price") or leg.get("entry_price") or 0.0
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
            entry_long_price = (
                float(
                    long_leg.get("avg_price")
                    or long_leg.get("price")
                    or long_leg.get("entry_price")
                    or 0.0
                )
                or None
            )
        if short_leg:
            entry_short_price = (
                float(
                    short_leg.get("avg_price")
                    or short_leg.get("price")
                    or short_leg.get("entry_price")
                    or 0.0
                )
                or None
            )
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
            strategy=STRATEGY_NAME,
        )
        return record
    except Exception:  # pragma: no cover - defensive persistence
        return None


def execute_hedged_trade(
    symbol: str, notion_usdt: float, leverage: float, min_spread: float
) -> dict:
    with slo.order_cycle_timer():

        spread_info = check_spread(symbol)
        spread_value = float(spread_info["spread"])
        risk_manager = get_strategy_risk_manager()

        def _record_failure(reason: str) -> None:
            try:
                risk_manager.record_failure(STRATEGY_NAME, reason)
            except Exception:
                LOGGER.exception(
                    "failed to record strategy failure",
                    extra={"strategy": STRATEGY_NAME, "reason": reason},
                )

        if spread_value < float(min_spread):
            _record_failure("spread_below_threshold")
            return {
                "symbol": spread_info["symbol"],
                "min_spread": float(min_spread),
                "spread": spread_value,
                "success": False,
                "reason": "spread_below_threshold",
                "details": spread_info,
            }

        notional = float(notion_usdt)
        leverage_value = float(leverage)
        cheap_price = _coerce_float(spread_info.get("cheap_mark"))
        expensive_price = _coerce_float(spread_info.get("expensive_mark"))
        long_size = notional / cheap_price if cheap_price > 0 else 0.0
        short_size = notional / expensive_price if expensive_price > 0 else 0.0

        long_plan = dict(choose_venue("long", spread_info["symbol"], long_size) or {})
        short_plan = dict(choose_venue("short", spread_info["symbol"], short_size) or {})

        dry_run_mode = is_dry_run_mode()
        budget_manager = get_strategy_budget_manager()

        if not risk_manager.is_enabled(STRATEGY_NAME):
            return {
                "ok": False,
                "executed": False,
                "state": "DISABLED_BY_OPERATOR",
                "reason": "disabled_by_operator",
                "strategy": STRATEGY_NAME,
            }
        if risk_manager.is_frozen(STRATEGY_NAME):
            record_risk_skip(STRATEGY_NAME, "strategy_frozen")
            slo.inc_skipped("hold")
            return {
                "ok": False,
                "executed": False,
                "state": "SKIPPED_BY_RISK",
                "reason": "strategy_frozen",
                "strategy": STRATEGY_NAME,
            }

        def _record_success() -> None:
            try:
                risk_manager.record_success(STRATEGY_NAME)
            except Exception:
                LOGGER.exception(
                    "failed to record strategy success",
                    extra={"strategy": STRATEGY_NAME},
                )

        def _record_and_return(reason: str, *, record_failure: bool = True) -> dict:
            if record_failure:
                _record_failure(reason)
            _record_execution_stat(
                symbol=spread_info["symbol"],
                side="long",
                plan=long_plan,
                leg=None,
                success=False,
                dry_run=dry_run_mode,
                failure_reason=reason,
            )
            _record_execution_stat(
                symbol=spread_info["symbol"],
                side="short",
                plan=short_plan,
                leg=None,
                success=False,
                dry_run=dry_run_mode,
                failure_reason=reason,
            )
            return {
                "symbol": spread_info["symbol"],
                "min_spread": float(min_spread),
                "spread": spread_value,
                "success": False,
                "reason": reason,
                "details": spread_info,
                "long_plan": long_plan,
                "short_plan": short_plan,
            }

        if not dry_run_mode and not budget_manager.can_allocate(
            STRATEGY_NAME, notional, requested_positions=1
        ):
            result = _record_and_return("strategy_budget_exceeded", record_failure=False)
            result.update(
                {
                    "ok": False,
                    "state": "BUDGET_BLOCKED",
                    "strategy": STRATEGY_NAME,
                    "limits": budget_manager.get_limits(STRATEGY_NAME),
                    "requested_notional": notional,
                }
            )
            return result

        if not long_plan or not short_plan:
            return _record_and_return("routing_unavailable")

        if (
            _coerce_float(long_plan.get("expected_fill_px")) <= 0.0
            or _coerce_float(short_plan.get("expected_fill_px")) <= 0.0
        ):
            return _record_and_return("quote_unavailable")

        if not bool(long_plan.get("liquidity_ok", True)) or not bool(
            short_plan.get("liquidity_ok", True)
        ):
            return _record_and_return("insufficient_liquidity")

        cheap_exchange = str(long_plan.get("venue") or spread_info["cheap"])
        expensive_exchange = str(short_plan.get("venue") or spread_info["expensive"])

        guard_allowed, guard_reason = guard_allowed_to_trade(spread_info["symbol"])
        if not guard_allowed:
            guard_code = f"edge_guard:{guard_reason or 'blocked'}"
            _record_failure(guard_code)
            _record_execution_stat(
                symbol=spread_info["symbol"],
                side="long",
                plan=long_plan,
                leg=None,
                success=False,
                dry_run=dry_run_mode,
                failure_reason=guard_code,
            )
            _record_execution_stat(
                symbol=spread_info["symbol"],
                side="short",
                plan=short_plan,
                leg=None,
                success=False,
                dry_run=dry_run_mode,
                failure_reason=guard_code,
            )
            _log_edge_guard_block(
                symbol=spread_info["symbol"],
                reason=guard_reason,
                spread_info=spread_info,
                long_plan=long_plan,
                short_plan=short_plan,
            )
            return {
                "symbol": spread_info["symbol"],
                "min_spread": float(min_spread),
                "spread": spread_value,
                "success": False,
                "reason": guard_code,
                "details": spread_info,
                "long_plan": long_plan,
                "short_plan": short_plan,
                "edge_guard_reason": guard_reason,
            }

        long_client = _client_for(cheap_exchange)
        short_client = _client_for(expensive_exchange)

        long_price_hint = _coerce_float(long_plan.get("expected_fill_px")) or cheap_price
        short_price_hint = _coerce_float(short_plan.get("expected_fill_px")) or expensive_price

        long_leg = None
        short_leg = None
        error_reason: str | None = None
        result: Dict[str, Any] | None = None

        try:
            register_order_attempt(reason="runaway_orders_per_min", source="cross_exchange_long")
            if dry_run_mode:
                long_leg = _simulate_leg(
                    exchange=cheap_exchange,
                    symbol=spread_info["symbol"],
                    side="long",
                    notional_usdt=notional,
                    leverage=leverage_value,
                    price=long_price_hint,
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
                    fallback_price=long_price_hint,
                )

            register_order_attempt(reason="runaway_orders_per_min", source="cross_exchange_short")
            if dry_run_mode:
                short_leg = _simulate_leg(
                    exchange=expensive_exchange,
                    symbol=spread_info["symbol"],
                    side="short",
                    notional_usdt=notional,
                    leverage=leverage_value,
                    price=short_price_hint,
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
                    fallback_price=short_price_hint,
                )
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
                "long_plan": long_plan,
                "short_plan": short_plan,
                "success": True,
                "status": "simulated" if dry_run_mode else "executed",
                "dry_run_mode": dry_run_mode,
                "simulated": dry_run_mode,
                "details": spread_info,
            }
        except HoldActiveError as exc:
            error_reason = exc.reason
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
            slo.inc_skipped("hold")
            result = {
                "symbol": spread_info["symbol"],
                "min_spread": float(min_spread),
                "spread": spread_value,
                "success": False,
                "reason": exc.reason,
                "details": spread_info,
                "hold_active": True,
                "partial_position": partial_record,
                "long_plan": long_plan,
                "short_plan": short_plan,
            }
            _record_failure(exc.reason or "hold_active")
        except Exception as exc:
            reason = str(exc)
            error_reason = reason
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
            result = {
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
                "long_plan": long_plan,
                "short_plan": short_plan,
            }
            _record_failure(result.get("reason") or reason or "order_failed")
        finally:
            long_success = _leg_successful(long_leg)
            short_success = _leg_successful(short_leg)
            _record_execution_stat(
                symbol=spread_info["symbol"],
                side="long",
                plan=long_plan,
                leg=long_leg,
                success=long_success,
                dry_run=dry_run_mode,
                failure_reason=None if long_success else error_reason,
            )
            _record_execution_stat(
                symbol=spread_info["symbol"],
                side="short",
                plan=short_plan,
                leg=short_leg,
                success=short_success,
                dry_run=dry_run_mode,
                failure_reason=None if short_success else error_reason,
            )

        final_result = result or {
            "symbol": spread_info["symbol"],
            "min_spread": float(min_spread),
            "spread": spread_value,
            "success": False,
            "reason": error_reason or "execution_failed",
            "details": spread_info,
            "long_plan": long_plan,
            "short_plan": short_plan,
        }
        if final_result.get("success") and not final_result.get("simulated"):
            _record_success()
        return final_result
