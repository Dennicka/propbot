from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import Broker
from .paper import PaperBroker
from .. import ledger
from ..metrics.observability import record_order_error
from ..services.runtime import get_state


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric > 1e12:
        numeric /= 1000.0
    dt = datetime.fromtimestamp(numeric, tz=timezone.utc)
    return dt.isoformat()


LOGGER = logging.getLogger(__name__)
_REQUEST_TIMEOUT = 5.0
_MAX_RETRIES = 3
_RETRY_BACKOFF = 0.5


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
        self._request_timeout = _REQUEST_TIMEOUT
        self._max_retries = _MAX_RETRIES
        self._retry_backoff = _RETRY_BACKOFF

    def metrics_tags(self) -> Dict[str, str]:  # pragma: no cover - simple mapping
        return {"broker": getattr(self, "venue", getattr(self, "name", "testnet"))}

    def emit_order_error(self, venue: str | None, reason: str | None) -> None:
        record_order_error(venue or self.venue, reason)

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

    async def _invoke_with_retries(self, func: Any, **params: Any) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(func, **params),
                    timeout=self._request_timeout,
                )
                return
            except Exception as exc:  # pragma: no cover - defensive logging
                last_error = exc
                LOGGER.warning(
                    "testnet call failed", extra={"attempt": attempt, "error": str(exc)}
                )
                if attempt >= self._max_retries:
                    break
                await asyncio.sleep(self._retry_backoff * attempt)
        if last_error:
            raise last_error

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
        try:
            await self._invoke_with_retries(client.place_order, **params)
            await asyncio.to_thread(ledger.update_order_status, order_id, "open")
        except Exception as exc:
            self.emit_order_error(venue or self.venue, exc.__class__.__name__)
            await asyncio.to_thread(ledger.update_order_status, order_id, "failed")
            ledger.record_event(
                level="ERROR",
                code="testnet_order_error",
                payload={
                    "venue": venue or self.venue,
                    "order_id": order_id,
                    "error": str(exc),
                },
            )
            raise
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
        if order and order.get("idemp_key"):
            params["client_order_id"] = order["idemp_key"]
        try:
            await self._invoke_with_retries(client.cancel_order, **params)
            await asyncio.to_thread(ledger.update_order_status, order_id, "cancelled")
        except Exception as exc:
            ledger.record_event(
                level="ERROR",
                code="testnet_cancel_error",
                payload={
                    "venue": venue or self.venue,
                    "order_id": order_id,
                    "error": str(exc),
                },
            )
            raise
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

    def _normalise_symbol(self, symbol: str | None) -> str:
        return str(symbol or "").upper()

    async def get_positions(self) -> List[Dict[str, object]]:
        if not self._should_place():
            return await self._paper.get_positions()
        client = self._client()
        if client is None:
            return await self._paper.get_positions()
        try:
            rows = await asyncio.to_thread(client.positions)
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.warning("failed to fetch testnet positions", extra={"venue": self.venue, "error": str(exc)})
            return await self._paper.get_positions()
        exposures: List[Dict[str, object]] = []
        for row in rows:
            symbol = self._normalise_symbol(row.get("symbol") or row.get("instId"))
            if not symbol:
                continue
            qty_raw = row.get("position_amt")
            if qty_raw is None:
                qty_raw = row.get("pos")
            if qty_raw is None:
                qty_raw = row.get("qty")
            try:
                qty = float(qty_raw)
            except (TypeError, ValueError):
                qty = 0.0
            if abs(qty) <= 1e-12:
                continue
            avg_raw = row.get("entry_price")
            if avg_raw is None:
                avg_raw = row.get("avgPx")
            if avg_raw is None:
                avg_raw = row.get("avg_price")
            try:
                avg_price = float(avg_raw)
            except (TypeError, ValueError):
                avg_price = 0.0
            exposures.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "qty": qty,
                    "avg_entry": avg_price,
                    "notional": abs(qty) * avg_price,
                }
            )
        return exposures

    async def get_fills(self, since: datetime | None = None) -> List[Dict[str, object]]:
        if not self._should_place():
            return await self._paper.get_fills(since=since)
        client = self._client()
        if client is None:
            return await self._paper.get_fills(since=since)
        since_ms: int | None = None
        if since is not None:
            since_ms = int(since.timestamp() * 1000)
        try:
            rows = await asyncio.to_thread(client.recent_fills, symbol=None, since=since_ms)
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.warning("failed to fetch testnet fills", extra={"venue": self.venue, "error": str(exc)})
            return await self._paper.get_fills(since=since)
        fills: List[Dict[str, object]] = []
        for row in rows:
            symbol = self._normalise_symbol(row.get("symbol") or row.get("instId"))
            side = str(row.get("side") or "").lower() or ("buy" if row.get("buy") else "sell")
            qty_raw = row.get("qty")
            if qty_raw is None:
                qty_raw = row.get("size")
            if qty_raw is None:
                qty_raw = row.get("fillSz")
            try:
                qty = abs(float(qty_raw))
            except (TypeError, ValueError):
                qty = 0.0
            price_raw = row.get("price")
            if price_raw is None:
                price_raw = row.get("fillPx")
            try:
                price = float(price_raw)
            except (TypeError, ValueError):
                price = 0.0
            fee_raw = row.get("fee")
            if fee_raw is None:
                fee_raw = row.get("commission")
            try:
                fee = abs(float(fee_raw))
            except (TypeError, ValueError):
                fee = 0.0
            ts_raw = row.get("time") or row.get("ts")
            ts = _to_iso(ts_raw) or _ts()
            fills.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "side": side or ("buy" if qty >= 0 else "sell"),
                    "qty": qty,
                    "price": price,
                    "fee": fee,
                    "ts": ts,
                }
            )
        return fills
