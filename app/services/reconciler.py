from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from .. import ledger
from ..broker.router import ExecutionRouter
from . import risk
from .runtime import get_state


LOGGER = logging.getLogger(__name__)


def _ts_from_payload(value) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            LOGGER.debug("reconciler could not parse timestamp value=%s error=%s", value, exc)
    return datetime.now(timezone.utc)


class FillReconciler:
    """Periodically poll brokers for fills and persist them into the ledger."""

    def __init__(self, router: ExecutionRouter | None = None) -> None:
        self.router = router or ExecutionRouter()
        self._last_fill_ts: datetime | None = None

    async def _record_fill(self, venue: str, payload: Dict[str, object]) -> Tuple[int, Dict[str, object]]:
        symbol = str(payload.get("symbol") or "")
        side = str(payload.get("side") or "buy").lower()
        qty = float(payload.get("qty", 0.0))
        price = float(payload.get("price", 0.0))
        fee = float(payload.get("fee", 0.0))
        ts_value = _ts_from_payload(payload.get("ts"))
        ts_iso = ts_value.astimezone(timezone.utc).isoformat()
        idemp_key = f"recon:{venue}:{symbol}:{side}:{qty}:{price}:{ts_iso}"
        order_id = await asyncio.to_thread(
            ledger.record_order,
            venue=venue,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            status="filled",
            client_ts=ts_iso,
            exchange_ts=ts_iso,
            idemp_key=idemp_key,
        )
        await asyncio.to_thread(
            ledger.record_fill,
            order_id=order_id,
            venue=venue,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            fee=fee,
            ts=ts_iso,
        )
        self._last_fill_ts = max(self._last_fill_ts or ts_value, ts_value)
        return order_id, {
            "order_id": order_id,
            "venue": venue,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "fee": fee,
            "ts": ts_iso,
        }

    async def run_once(self) -> Dict[str, object]:
        brokers = self.router.brokers()
        fills: List[Dict[str, object]] = []
        positions: List[Dict[str, object]] = []
        for venue, broker in brokers.items():
            try:
                broker_fills = await broker.get_fills(since=self._last_fill_ts)
            except Exception:  # pragma: no cover - defensive logging
                broker_fills = []
            for payload in broker_fills:
                ts_value = _ts_from_payload(payload.get("ts"))
                if self._last_fill_ts and ts_value <= self._last_fill_ts:
                    continue
                _, entry = await self._record_fill(venue, payload)
                fills.append(entry)
            try:
                venue_positions = await broker.get_positions()
            except Exception:  # pragma: no cover - defensive logging
                venue_positions = []
            for position in venue_positions:
                data = dict(position)
                data.setdefault("venue", venue)
                positions.append(data)

        pnl_totals = await asyncio.to_thread(ledger.compute_pnl)
        open_orders = await asyncio.to_thread(ledger.fetch_open_orders)
        risk.refresh_runtime_state(open_orders=open_orders)
        state = get_state()
        state.metrics.counters["reconciled_fills"] = state.metrics.counters.get(
            "reconciled_fills", 0
        ) + len(fills)
        return {"fills": fills, "positions": positions, "pnl": pnl_totals}


__all__ = ["FillReconciler"]

