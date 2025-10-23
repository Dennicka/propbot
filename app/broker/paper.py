from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Dict

from .base import Broker
from .. import ledger


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaperBroker(Broker):
    """Simple broker that instantly fills orders and records them in the ledger."""

    def __init__(self, venue: str) -> None:
        self.venue = venue

    async def create_order(
        self,
        *,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: float | None = None,
        type: str = "LIMIT",
        post_only: bool = True,
        reduce_only: bool = False,
        fee: float = 0.0,
        idemp_key: str | None = None,
    ) -> Dict[str, object]:
        venue_id = venue or self.venue
        key = idemp_key or f"paper-{uuid.uuid4().hex}"
        price_value = float(price) if price is not None else 0.0
        qty_value = float(qty)
        side_value = side.lower()
        client_ts = _ts()
        order_id = await asyncio.to_thread(
            ledger.record_order,
            venue=venue_id,
            symbol=symbol,
            side=side_value,
            qty=qty_value,
            price=price_value,
            status="filled",
            client_ts=client_ts,
            exchange_ts=client_ts,
            idemp_key=key,
        )
        await asyncio.to_thread(
            ledger.record_fill,
            order_id=order_id,
            venue=venue_id,
            symbol=symbol,
            side=side_value,
            qty=qty_value,
            price=price_value,
            fee=float(fee),
            ts=client_ts,
        )
        await asyncio.to_thread(ledger.update_order_status, order_id, "filled")
        return {
            "order_id": order_id,
            "venue": venue_id,
            "symbol": symbol,
            "side": side_value,
            "qty": qty_value,
            "price": price_value,
            "fee": float(fee),
            "type": type,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "ts": client_ts,
            "idemp_key": key,
        }

    async def cancel(self, *, venue: str, order_id: int) -> None:
        await asyncio.to_thread(ledger.update_order_status, order_id, "cancelled")

    async def positions(self, *, venue: str) -> Dict[str, object]:
        rows = await asyncio.to_thread(ledger.fetch_positions)
        return {"positions": [row for row in rows if row["venue"] == (venue or self.venue)]}

    async def balances(self, *, venue: str) -> Dict[str, object]:
        rows = await asyncio.to_thread(ledger.fetch_balances)
        return {"balances": [row for row in rows if row["venue"] == (venue or self.venue)]}

    async def get_positions(self) -> list[Dict[str, object]]:
        rows = await asyncio.to_thread(ledger.fetch_positions)
        exposures: list[Dict[str, object]] = []
        for row in rows:
            qty = float(row.get("base_qty", 0.0))
            avg_price = float(row.get("avg_price", 0.0))
            if abs(qty) <= 1e-12:
                continue
            exposures.append(
                {
                    "venue": row.get("venue", self.venue),
                    "symbol": row.get("symbol"),
                    "qty": qty,
                    "avg_entry": avg_price,
                    "notional": abs(qty) * avg_price,
                }
            )
        return exposures

    async def get_fills(self, since: datetime | None = None) -> list[Dict[str, object]]:
        rows = await asyncio.to_thread(ledger.fetch_fills_since, since)
        fills: list[Dict[str, object]] = []
        for row in rows:
            fills.append(
                {
                    "venue": row.get("venue", self.venue),
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "qty": float(row.get("qty", 0.0)),
                    "price": float(row.get("price", 0.0)),
                    "fee": float(row.get("fee", 0.0)),
                    "ts": row.get("ts"),
                }
            )
        return fills
