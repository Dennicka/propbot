from __future__ import annotations

import asyncio
import hashlib
import json
from typing import TYPE_CHECKING, Dict, List

from .base import Broker
from .paper import PaperBroker
from .testnet import TestnetBroker
from .. import ledger
from ..services import portfolio
from ..services.runtime import get_state, set_open_orders

if TYPE_CHECKING:  # pragma: no cover
    from ..services.arbitrage import Plan


VENUE_ALIASES: Dict[str, str] = {
    "binance": "binance-um",
    "binance-um": "binance-um",
    "binance_um": "binance-um",
    "okx": "okx-perp",
    "okx-perp": "okx-perp",
    "okx_perp": "okx-perp",
    "paper": "paper",
}


class ExecutionRouter:
    def __init__(self) -> None:
        state = get_state()
        self.safe_mode = state.control.safe_mode
        self.dry_run_only = state.control.dry_run
        self.two_man_rule = state.control.two_man_rule
        self._brokers: Dict[str, Broker] = {
            "paper": PaperBroker("paper"),
            "binance-um": TestnetBroker(
                "binance-um",
                "binance_um",
                safe_mode=self.safe_mode or self.dry_run_only,
                required_env=("BINANCE_UM_API_KEY_TESTNET", "BINANCE_UM_API_SECRET_TESTNET"),
            ),
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
        return await self._dispatch_plan(plan)

    async def _simulate_plan(self, plan: "Plan") -> Dict[str, object]:
        return await self._dispatch_plan(plan)

    async def _dispatch_plan(self, plan: "Plan") -> Dict[str, object]:
        orders: List[Dict[str, object]] = []
        plan_payload = plan.as_dict()
        plan_key = hashlib.sha256(json.dumps(plan_payload, sort_keys=True).encode("utf-8")).hexdigest()
        for index, leg in enumerate(plan.legs):
            broker = self._resolve_broker(leg.exchange)
            venue = self._venue_for_exchange(leg.exchange)
            idemp_key = f"{plan_key}:{index}"
            order = await broker.create_order(
                venue=venue,
                symbol=plan.symbol,
                side=leg.side,
                qty=leg.qty,
                price=leg.price,
                type="LIMIT",
                post_only=True,
                reduce_only=False,
                fee=leg.fee_usdt,
                idemp_key=idemp_key,
            )
            orders.append(order)
        snapshot = await portfolio.snapshot()
        open_orders = await self._refresh_open_orders()
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
