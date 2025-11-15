"""Facade for loading internal state snapshots for reconciliation."""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from app.ledger import fetch_balances, fetch_open_orders, fetch_positions
from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
    VenueId,
)


def _to_decimal(value: object, *, default: Decimal | None = Decimal("0")) -> Decimal | None:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):  # pragma: no cover - defensive
        return default


def _first_decimal(
    mapping: Mapping[str, object], *keys: str, default: Decimal = Decimal("0")
) -> Decimal:
    for key in keys:
        if key not in mapping:
            continue
        result = _to_decimal(mapping.get(key), default=None)
        if result is not None:
            return result
    return default


def _normalise_side(value: object) -> str:
    token = str(value or "").strip().lower()
    if token in {"buy", "bid", "long"}:
        return "buy"
    if token in {"sell", "ask", "short"}:
        return "sell"
    return "buy"


class InternalStateSource:
    """Facade to load internal balances/positions/orders for reconciliation."""

    async def load_balances(self, venue_id: VenueId) -> Sequence[ExchangeBalanceSnapshot]:
        rows = await asyncio.to_thread(fetch_balances)
        snapshots: list[ExchangeBalanceSnapshot] = []
        for row in rows:
            data = dict(row)
            venue = str(data.get("venue") or "")
            if venue != str(venue_id):
                continue
            asset_raw = data.get("asset") or data.get("currency") or data.get("symbol") or ""
            asset = str(asset_raw).upper()
            if not asset:
                continue
            total = _first_decimal(data, "total", "qty", "balance")
            available = _first_decimal(data, "free", "available", "qty")
            snapshots.append(
                ExchangeBalanceSnapshot(
                    venue_id=venue,
                    asset=asset,
                    total=total,
                    available=available,
                )
            )
        return snapshots

    async def load_positions(self, venue_id: VenueId) -> Sequence[ExchangePositionSnapshot]:
        rows = await asyncio.to_thread(fetch_positions)
        snapshots: list[ExchangePositionSnapshot] = []
        for row in rows:
            data = dict(row)
            venue = str(data.get("venue") or "")
            if venue != str(venue_id):
                continue
            symbol_raw = data.get("symbol") or ""
            symbol = str(symbol_raw).upper()
            if not symbol:
                continue
            qty = _first_decimal(data, "base_qty", "qty", "size")
            entry_price = _to_decimal(data.get("avg_price"), default=None)
            notional = qty.copy_abs() * entry_price if entry_price is not None else Decimal("0")
            snapshots.append(
                ExchangePositionSnapshot(
                    venue_id=venue,
                    symbol=symbol,
                    qty=qty,
                    entry_price=entry_price,
                    notional=notional,
                )
            )
        return snapshots

    async def load_open_orders(self, venue_id: VenueId) -> Sequence[ExchangeOrderSnapshot]:
        rows = await asyncio.to_thread(fetch_open_orders)
        snapshots: list[ExchangeOrderSnapshot] = []
        for row in rows:
            data = dict(row)
            venue = str(data.get("venue") or "")
            if venue != str(venue_id):
                continue
            symbol_raw = data.get("symbol") or ""
            symbol = str(symbol_raw).upper()
            if not symbol:
                continue
            qty = _first_decimal(data, "qty", "size")
            price = _first_decimal(data, "price", "px")
            side = _normalise_side(data.get("side"))
            status = str(data.get("status") or "").strip().lower() or "unknown"
            client_order_id_raw = (
                data.get("idemp_key")
                or data.get("client_order_id")
                or data.get("order_id")
                or data.get("id")
            )
            client_order_id = str(client_order_id_raw) if client_order_id_raw else None
            exchange_order_id_raw = data.get("exchange_order_id") or data.get("exch_order_id")
            exchange_order_id = str(exchange_order_id_raw) if exchange_order_id_raw else None
            snapshots.append(
                ExchangeOrderSnapshot(
                    venue_id=venue,
                    symbol=symbol,
                    client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id,
                    side=side,
                    qty=qty,
                    price=price,
                    status=status,
                )
            )
        return snapshots


__all__ = ["InternalStateSource"]
