from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Iterable, List, Mapping, Sequence

from .base import Broker
from .binance import BinanceLiveBroker, BinanceTestnetBroker
from .paper import PaperBroker
from .testnet import TestnetBroker
from .. import ledger, risk_governor
from ..services import portfolio, risk
from ..golden.recorder import golden_replay_enabled
from ..services.runtime import (
    HoldActiveError,
    get_market_data,
    get_safety_status,
    get_state,
    is_hold_active,
    register_order_attempt,
    set_open_orders,
)
from ..runtime.pre_trade_gate import enforce_pre_trade
from ..risk.risk_governor import (
    get_pretrade_risk_governor,
    record_order_error as record_risk_order_error,
    record_order_success as record_risk_order_success,
)
from ..watchdog.broker_watchdog import get_broker_watchdog
from ..util.venues import VENUE_ALIASES

if TYPE_CHECKING:  # pragma: no cover
    from ..services.arbitrage import Plan


ORDER_TIMEOUT_SEC = 2.0
MAX_ORDER_ATTEMPTS = 3


LOGGER = logging.getLogger(__name__)


def _maybe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _batch_id_for_orders(orders: Iterable[Mapping[str, object]]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    hasher = hashlib.sha256()
    for entry in sorted(
        (
            (
                str(order.get("venue") or "").lower(),
                int(order.get("id", 0)),
                str(order.get("symbol") or "").upper(),
                str(order.get("idemp_key") or ""),
            )
            for order in orders
        ),
        key=lambda item: (item[0], item[1]),
    ):
        venue, order_id, symbol, idemp_key = entry
        hasher.update(str(order_id).encode("utf-8"))
        if venue:
            hasher.update(venue.encode("utf-8"))
        if symbol:
            hasher.update(symbol.encode("utf-8"))
        if idemp_key:
            hasher.update(idemp_key.encode("utf-8"))
    digest = hasher.hexdigest()[:12]
    return f"cancel-{timestamp}-{digest}"


class ExecutionRouter:
    def __init__(self) -> None:
        state = get_state()
        self.safe_mode = state.control.safe_mode
        self.dry_run_only = state.control.dry_run
        self.two_man_rule = state.control.two_man_rule
        self.market_data = get_market_data()
        environment = str(
            state.control.environment or state.control.deployment_mode or "paper"
        ).lower()
        if environment == "testnet":
            binance_broker = BinanceTestnetBroker(
                venue="binance-um",
                safe_mode=self.safe_mode,
                dry_run=self.dry_run_only,
            )
        elif environment == "live":
            binance_broker = BinanceLiveBroker(
                venue="binance-um",
                safe_mode=self.safe_mode,
                dry_run=self.dry_run_only,
            )
        else:
            binance_broker = PaperBroker("binance-um")

        self._brokers: Dict[str, Broker] = {
            "paper": PaperBroker("paper"),
            "binance-um": binance_broker,
            "okx-perp": TestnetBroker(
                "okx-perp",
                "okx_perp",
                safe_mode=self.safe_mode or self.dry_run_only,
                required_env=(
                    ("OKX_TESTNET_API_KEY", "OKX_API_KEY_TESTNET"),
                    ("OKX_TESTNET_API_SECRET", "OKX_API_SECRET_TESTNET"),
                    (
                        "OKX_TESTNET_API_PASSPHRASE",
                        "OKX_API_PASSPHRASE_TESTNET",
                    ),
                ),
            ),
            "bybit-perp": TestnetBroker(
                "bybit-perp",
                "bybit_perp",
                safe_mode=self.safe_mode or self.dry_run_only,
                required_env=(
                    ("BYBIT_TESTNET_API_KEY", "BYBIT_API_KEY_TESTNET"),
                    ("BYBIT_TESTNET_API_SECRET", "BYBIT_API_SECRET_TESTNET"),
                ),
            ),
        }
        self._watchdog = get_broker_watchdog()

    def _resolve_broker(self, exchange: str) -> Broker:
        canonical = VENUE_ALIASES.get(exchange.lower(), exchange.lower())
        if self.dry_run_only:
            return self._brokers["paper"]
        return self._brokers.get(canonical, self._brokers["paper"])

    def _venue_for_exchange(self, exchange: str) -> str:
        return VENUE_ALIASES.get(exchange.lower(), exchange.lower())

    def broker_for_venue(self, venue: str) -> Broker:
        canonical = VENUE_ALIASES.get(venue.lower(), venue.lower())
        if self.dry_run_only:
            return self._brokers["paper"]
        return self._brokers.get(canonical, self._brokers["paper"])

    def brokers(self) -> Dict[str, Broker]:
        return dict(self._brokers)

    async def cancel_all(
        self,
        *,
        venue: str,
        orders: Sequence[Mapping[str, object]] | None = None,
        batch_id: str | None = None,
    ) -> Dict[str, object]:
        canonical = VENUE_ALIASES.get(venue.lower(), venue.lower())
        broker = self.broker_for_venue(canonical)
        if orders is None:
            fetched = await asyncio.to_thread(ledger.fetch_open_orders)
            venue_orders = [
                order for order in fetched if str(order.get("venue") or "").lower() == canonical
            ]
        else:
            venue_orders = [dict(order) for order in orders]
        if not venue_orders:
            return {
                "venue": canonical,
                "batch_id": batch_id,
                "cancelled": 0,
                "failed": 0,
                "skipped": True,
            }
        batch = batch_id or _batch_id_for_orders(venue_orders)
        cancelled = 0
        failed = 0
        cancel_method = getattr(broker, "cancel_all", None)
        bulk_success = False
        if callable(cancel_method):
            try:
                maybe = cancel_method(symbol=None, batch_id=batch)
            except TypeError:
                try:
                    maybe = cancel_method(venue=canonical, batch_id=batch)
                except TypeError:
                    maybe = cancel_method()
            result = await maybe if inspect.isawaitable(maybe) else maybe
            if isinstance(result, Mapping):
                cancelled = int(result.get("cancelled", 0) or 0)
                failed = int(result.get("failed", 0) or 0)
                bulk_success = cancelled >= len(venue_orders)
            else:
                cancelled = len(venue_orders)
                failed = 0
                bulk_success = True
        if not bulk_success:
            cancelled = 0
            failed = 0
            for order in venue_orders:
                order_id = int(order.get("id", 0))
                try:
                    await broker.cancel(venue=order.get("venue") or canonical, order_id=order_id)
                    cancelled += 1
                except Exception as exc:  # pragma: no cover - defensive logging
                    failed += 1
                    LOGGER.debug(
                        "cancel failed",
                        extra={"venue": canonical, "order_id": order_id, "error": str(exc)},
                    )
        if cancelled:
            await asyncio.gather(
                *(
                    asyncio.to_thread(
                        ledger.update_order_status, int(order.get("id", 0)), "cancelled"
                    )
                    for order in venue_orders
                )
            )
        return {
            "venue": canonical,
            "batch_id": batch,
            "cancelled": cancelled,
            "failed": failed,
        }

    def _nudge_price(self, *, venue: str, symbol: str, side: str, original: float) -> float:
        try:
            book = self.market_data.top_of_book(venue, symbol)
        except Exception:  # pragma: no cover - fallback to original
            return original
        best_bid = float(book.get("bid", 0.0))
        best_ask = float(book.get("ask", 0.0))
        epsilon = max(abs(original) * 1e-4, 1e-6)
        if side.lower() == "buy":
            if best_bid > 0:
                adjusted = min(original, best_bid - epsilon)
                if adjusted > 0:
                    return adjusted
        else:
            if best_ask > 0:
                adjusted = max(original, best_ask + epsilon)
                return adjusted
        return original

    async def _place_leg_with_retry(
        self,
        *,
        broker: Broker,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        fee: float,
        post_only: bool,
        reduce_only: bool,
        plan_key: str,
        index: int,
    ) -> Dict[str, object]:
        attempt = 0
        last_error: Exception | None = None
        while attempt < MAX_ORDER_ATTEMPTS:
            attempt_key = f"{plan_key}:{index}" if attempt == 0 else f"{plan_key}:{index}:{attempt}"
            price_to_use = price
            if post_only and attempt > 0:
                price_to_use = self._nudge_price(
                    venue=venue, symbol=symbol, side=side, original=price_to_use
                )
            try:
                price_for_notional = price_to_use if price_to_use is not None else price
                try:
                    notional_value = abs(float(qty) * float(price_for_notional or 0.0))
                except (TypeError, ValueError):
                    notional_value = 0.0
                order_context = {
                    "operation": "order",
                    "venue": venue,
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "price": price_to_use,
                    "notional": notional_value,
                    "positions_delta": 0 if reduce_only else 1,
                }
                enforce_pre_trade(venue, order_context)
                context_qty = order_context.get("qty")
                context_price = order_context.get("price")
                if context_qty is not None:
                    converted_qty = _maybe_float(context_qty)
                    if converted_qty is not None:
                        qty = converted_qty
                if context_price is not None:
                    converted_price = _maybe_float(context_price)
                    if converted_price is not None:
                        price_to_use = converted_price
                if self._watchdog.should_block_orders(venue):
                    raise HoldActiveError("WATCHDOG_DOWN")
                self._watchdog.record_order_submit(venue)
                register_order_attempt(
                    reason="runaway_orders_per_min",
                    source=f"execution_router:{venue}:{side.lower()}",
                )
                create_task = broker.create_order(
                    venue=venue,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    price=price_to_use,
                    type="LIMIT",
                    post_only=post_only,
                    reduce_only=reduce_only,
                    fee=fee,
                    idemp_key=attempt_key,
                )
                order = await asyncio.wait_for(create_task, timeout=ORDER_TIMEOUT_SEC)
                record_risk_order_success(venue=venue, category="accepted")
                return order
            except HoldActiveError:
                raise
            except asyncio.TimeoutError as exc:
                last_error = exc
                record_risk_order_error(venue=venue, category="timeout")
            except Exception as exc:  # pragma: no cover - defensive logging
                last_error = exc
                record_risk_order_error(
                    venue=venue,
                    category=getattr(exc, "__class__", type(exc)).__name__,
                )
            attempt += 1
            if attempt < MAX_ORDER_ATTEMPTS:
                await asyncio.sleep(min(0.2 * (2**attempt), 1.0))
        message = "order_failed"
        if last_error:
            message = f"order_failed:{last_error}"
        ledger.record_event(
            level="ERROR",
            code="order_execution_failed",
            payload={
                "venue": venue,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "price": price,
                "post_only": post_only,
                "error": str(last_error) if last_error else "timeout",
            },
        )
        raise RuntimeError(message)

    async def _refresh_open_orders(self) -> List[Dict[str, object]]:
        orders = await asyncio.to_thread(ledger.fetch_open_orders)
        set_open_orders(orders)
        return orders

    async def execute_plan(
        self, plan: "Plan", *, allow_safe_mode: bool = False
    ) -> Dict[str, object]:
        state = get_state()
        if state.control.safe_mode:
            if allow_safe_mode:
                return await self._simulate_plan(plan)
            if state.control.dry_run:
                return await self._simulate_plan(plan)
            raise PermissionError("SAFE_MODE blocks execution")
        if state.control.dry_run:
            return await self._simulate_plan(plan)
        if state.control.two_man_rule and len(state.control.approvals) < 2:
            raise PermissionError("TWO_MAN_RULE approvals missing")
        risk_state = risk.refresh_runtime_state()
        if risk_state.breaches:
            reasons = [breach.detail or breach.limit for breach in risk_state.breaches]
            raise PermissionError("RISK_BREACH: " + "; ".join(reasons))
        return await self._dispatch_plan(plan)

    async def _simulate_plan(self, plan: "Plan") -> Dict[str, object]:
        return await self._dispatch_plan(plan, simulate=True)

    async def _dispatch_plan(self, plan: "Plan", simulate: bool = False) -> Dict[str, object]:
        orders: List[Dict[str, object]] = []
        plan_payload = plan.as_dict()
        plan_key = hashlib.sha256(
            json.dumps(plan_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        state = get_state()
        post_only = bool(state.control.post_only)
        reduce_only = bool(state.control.reduce_only)
        shadow_mode = golden_replay_enabled()
        if (
            not simulate
            and not self.dry_run_only
            and not state.control.safe_mode
            and is_hold_active()
        ):
            safety = get_safety_status()
            raise HoldActiveError(safety.get("hold_reason") or "hold_active")
        if simulate or self.dry_run_only or state.control.safe_mode or shadow_mode:
            for leg in plan.legs:
                venue = self._venue_for_exchange(leg.exchange)
                orders.append(
                    {
                        "venue": venue,
                        "symbol": plan.symbol,
                        "side": leg.side,
                        "qty": leg.qty,
                        "price": leg.price,
                        "simulated": True,
                    }
                )
            open_orders = await self._refresh_open_orders()
        else:
            hold_reason = await risk_governor.validate(context="order_execution")
            if hold_reason:
                raise HoldActiveError(hold_reason)
            for index, leg in enumerate(plan.legs):
                broker = self._resolve_broker(leg.exchange)
                venue = self._venue_for_exchange(leg.exchange)
                order = await self._place_leg_with_retry(
                    broker=broker,
                    venue=venue,
                    symbol=plan.symbol,
                    side=leg.side,
                    qty=leg.qty,
                    price=leg.price,
                    fee=leg.fee_usdt,
                    post_only=post_only,
                    reduce_only=reduce_only,
                    plan_key=plan_key,
                    index=index,
                )
                orders.append(order)
            open_orders = await self._refresh_open_orders()
        snapshot = await portfolio.snapshot()
        risk.refresh_runtime_state(snapshot=snapshot, open_orders=open_orders)
        return {
            "orders": orders,
            "exposures": snapshot.exposures(),
            "pnl": dict(snapshot.pnl_totals),
            "portfolio": snapshot.as_dict(),
            "open_orders": open_orders,
        }

    async def place_limit_order(
        self,
        *,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        client_order_id: str | None = None,
        post_only: bool = True,
        reduce_only: bool = False,
    ) -> Dict[str, object]:
        broker = self.broker_for_venue(venue)
        order = await broker.create_order(
            venue=venue,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            type="LIMIT",
            post_only=post_only,
            reduce_only=reduce_only,
            idemp_key=client_order_id,
        )
        await self._refresh_open_orders()
        return order

    async def cancel_order(self, *, venue: str, order_id: int) -> None:
        governor = get_pretrade_risk_governor()
        ok, reason = governor.check_and_account(None, {"operation": "cancel", "venue": venue})
        if not ok:
            raise HoldActiveError(reason or "RISK_THROTTLED")
        broker = self.broker_for_venue(venue)
        await broker.cancel(venue=venue, order_id=order_id)
        await self._refresh_open_orders()

    async def replace_limit_order(
        self,
        *,
        venue: str,
        order_id: int,
        price: float,
        symbol: str | None = None,
        side: str | None = None,
        qty: float | None = None,
        client_order_id: str | None = None,
        post_only: bool = True,
        reduce_only: bool = False,
    ) -> Dict[str, object]:
        existing = await asyncio.to_thread(ledger.get_order, order_id)
        if not existing:
            raise ValueError(f"order {order_id} not found")
        symbol_value = symbol or str(existing.get("symbol") or "")
        side_value = (side or str(existing.get("side") or "")).lower()
        if not symbol_value or side_value not in {"buy", "sell"}:
            raise ValueError("symbol and side must be provided")
        qty_value = qty if qty is not None else float(existing.get("qty") or 0.0)
        if qty_value <= 0:
            raise ValueError("qty must be positive")
        broker = self.broker_for_venue(venue)
        governor = get_pretrade_risk_governor()
        try:
            notional_value = abs(float(qty_value) * float(price))
        except (TypeError, ValueError):
            notional_value = 0.0
        ok, reason = governor.check_and_account(
            None,
            {
                "operation": "replace",
                "venue": venue,
                "symbol": symbol_value,
                "notional": notional_value,
                "positions_delta": 0,
            },
        )
        if not ok:
            raise HoldActiveError(reason or "RISK_THROTTLED")
        await broker.cancel(venue=venue, order_id=order_id)
        replacement_id = client_order_id or f"{existing.get('idemp_key') or order_id}:replace"
        order = await broker.create_order(
            venue=venue,
            symbol=symbol_value,
            side=side_value,
            qty=qty_value,
            price=price,
            type="LIMIT",
            post_only=post_only,
            reduce_only=reduce_only,
            idemp_key=replacement_id,
        )
        await self._refresh_open_orders()
        return order
