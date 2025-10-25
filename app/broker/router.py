from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Dict, List

from .base import Broker
from .binance import BinanceLiveBroker, BinanceTestnetBroker
from .paper import PaperBroker
from .testnet import TestnetBroker
from .. import ledger
from ..services import portfolio, risk
from ..services.runtime import get_market_data, get_state, set_open_orders
from ..util.venues import VENUE_ALIASES

if TYPE_CHECKING:  # pragma: no cover
    from ..services.arbitrage import Plan


ORDER_TIMEOUT_SEC = 2.0
MAX_ORDER_ATTEMPTS = 3


LOGGER = logging.getLogger(__name__)


class ExecutionRouter:
    def __init__(self) -> None:
        state = get_state()
        self.safe_mode = state.control.safe_mode
        self.dry_run_only = state.control.dry_run
        self.two_man_rule = state.control.two_man_rule
        self.market_data = get_market_data()
        environment = str(state.control.environment or state.control.deployment_mode or "paper").lower()
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
                required_env=("OKX_API_KEY_TESTNET", "OKX_API_SECRET_TESTNET", "OKX_API_PASSPHRASE_TESTNET"),
            ),
        }

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

    def _nudge_price(
        self, *, venue: str, symbol: str, side: str, original: float
    ) -> float:
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
                return order
            except asyncio.TimeoutError as exc:
                last_error = exc
            except Exception as exc:  # pragma: no cover - defensive logging
                last_error = exc
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

    async def execute_plan(self, plan: "Plan", *, allow_safe_mode: bool = False) -> Dict[str, object]:
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
        plan_key = hashlib.sha256(json.dumps(plan_payload, sort_keys=True).encode("utf-8")).hexdigest()
        state = get_state()
        post_only = bool(state.control.post_only)
        reduce_only = bool(state.control.reduce_only)
        if simulate or self.dry_run_only or state.control.safe_mode:
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
