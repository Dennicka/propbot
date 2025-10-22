from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from .base import Broker
from .paper import PaperBroker
from .. import ledger
from ..services.runtime import get_state


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestnetBroker(Broker):
    """Broker capable of routing to real testnet venues when permitted."""

    def __init__(
        self,
        venue: str,
        runtime_venue: str,
        *,
        safe_mode: bool,
        required_env: tuple[str, ...],
    ) -> None:
        self.venue = venue
        self.runtime_venue = runtime_venue
        self.safe_mode = safe_mode
        self.required_env = required_env
        self._paper = PaperBroker(venue)
        self._enable_place_orders = _env_flag("ENABLE_PLACE_TEST_ORDERS", False)
        if self._enable_place_orders and not safe_mode:
            self._ensure_credentials()

    def _ensure_credentials(self) -> None:
        missing = [name for name in self.required_env if not os.getenv(name)]
        if missing:
            missing_vars = ", ".join(missing)
            raise RuntimeError(f"missing credentials for {self.venue}: {missing_vars}")

    def _client(self):
        state = get_state()
        runtime = state.derivatives
        if not runtime:
            return None
        venue_runtime = runtime.venues.get(self.runtime_venue)
        if not venue_runtime:
            return None
        return venue_runtime.client

    def _should_place(self) -> bool:
        if self.safe_mode:
            return False
        if not self._enable_place_orders:
            return False
        return True

    async def create_order(
        self,
        *,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float] = None,
        type: str = "LIMIT",
        post_only: bool = True,
        reduce_only: bool = False,
        fee: float = 0.0,
        idemp_key: str | None = None,
    ) -> Dict[str, object]:
        if not self._should_place():
            return await self._paper.create_order(
                venue=venue,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                type=type,
                post_only=post_only,
                reduce_only=reduce_only,
                fee=fee,
                idemp_key=idemp_key,
            )

        client = self._client()
        if client is None:
            return await self._paper.create_order(
                venue=venue,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                type=type,
                post_only=post_only,
                reduce_only=reduce_only,
                fee=fee,
                idemp_key=idemp_key,
            )

        order_key = idemp_key or f"{self.venue}-{uuid.uuid4().hex}"
        ts = _ts()
        order_id = await asyncio.to_thread(
            ledger.record_order,
            venue=venue or self.venue,
            symbol=symbol,
            side=side.lower(),
            qty=float(qty),
            price=float(price or 0.0),
            status="submitted",
            client_ts=ts,
            exchange_ts=None,
            idemp_key=order_key,
        )
        params: Dict[str, object] = {
            "symbol": symbol,
            "side": side.upper(),
            "type": type.upper(),
            "quantity": qty,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "client_order_id": order_key,
        }
        if price is not None:
            params["price"] = price
        await asyncio.to_thread(client.place_order, **params)
        payload = {
            "order_id": order_id,
            "venue": venue or self.venue,
            "symbol": symbol,
            "side": side.lower(),
            "qty": float(qty),
            "price": float(price or 0.0),
            "fee": float(fee),
            "type": type,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "ts": ts,
            "idemp_key": order_key,
        }
        ledger.record_event(
            level="INFO",
            code="testnet_order_submitted",
            payload={"venue": payload["venue"], "symbol": symbol, "order_id": order_id},
        )
        return payload

    async def cancel(self, *, venue: str, order_id: int) -> None:
        if not self._should_place():
            await self._paper.cancel(venue=venue, order_id=order_id)
            return
        client = self._client()
        if client is None:
            await self._paper.cancel(venue=venue, order_id=order_id)
            return
        order = await asyncio.to_thread(ledger.get_order, order_id)
        symbol = order.get("symbol") if order else None
        params: Dict[str, object] = {}
        if symbol:
            params["symbol"] = symbol
        params["order_id"] = order_id
        await asyncio.to_thread(client.cancel_order, **params)
        await asyncio.to_thread(ledger.update_order_status, order_id, "cancelled")
        ledger.record_event(
            level="INFO",
            code="testnet_order_cancelled",
            payload={"venue": venue or self.venue, "order_id": order_id, "symbol": symbol},
        )

    async def positions(self, *, venue: str) -> Dict[str, object]:
        if not self._should_place():
            return await self._paper.positions(venue=venue)
        client = self._client()
        if client is None:
            return await self._paper.positions(venue=venue)
        rows = await asyncio.to_thread(client.positions)
        return {"positions": rows}

    async def balances(self, *, venue: str) -> Dict[str, object]:
        if not self._should_place():
            return await self._paper.balances(venue=venue)
        # Testnet balances are not exposed via the lightweight clients; return ledger snapshot
        return await self._paper.balances(venue=venue)
