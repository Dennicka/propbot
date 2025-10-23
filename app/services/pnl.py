from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class Fill:
    symbol: str
    qty: float
    price: float
    side: str
    fee: float = 0.0
    ts: datetime | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "Fill":
        symbol = str(payload.get("symbol") or "").upper()
        qty = float(payload.get("qty") or 0.0)
        price = float(payload.get("price") or 0.0)
        side = str(payload.get("side") or "").lower()
        fee = float(payload.get("fee") or 0.0)
        ts_raw = payload.get("ts")
        ts_value: datetime | None = None
        if isinstance(ts_raw, datetime):
            ts_value = ts_raw
        elif ts_raw:
            try:
                ts_value = datetime.fromisoformat(str(ts_raw))
            except ValueError:
                ts_value = None
        return cls(symbol=symbol, qty=qty, price=price, side=side, fee=fee, ts=ts_value)


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    avg_entry: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "Position":
        symbol = str(payload.get("symbol") or "").upper()
        qty = float(payload.get("qty") or 0.0)
        avg_entry = float(payload.get("avg_entry") or payload.get("avg_price") or 0.0)
        return cls(symbol=symbol, qty=qty, avg_entry=avg_entry)


def _sort_key(fill: Fill) -> tuple[int, float]:
    if fill.ts is None:
        return (1, 0.0)
    return (0, fill.ts.timestamp())


def compute_realized_pnl(fills: Sequence[Fill]) -> float:
    state: dict[str, dict[str, float]] = {}
    realized = 0.0
    for fill in sorted(fills, key=_sort_key):
        if not fill.symbol or fill.qty <= 0:
            realized -= fill.fee
            continue
        symbol_state = state.setdefault(fill.symbol, {"qty": 0.0, "avg": 0.0})
        signed_qty = fill.qty if fill.side == "buy" else -fill.qty
        position_qty = symbol_state["qty"]
        avg_price = symbol_state["avg"]
        if position_qty == 0.0 or position_qty * signed_qty > 0:
            new_qty = position_qty + signed_qty
            if new_qty == 0.0:
                symbol_state["qty"] = 0.0
                symbol_state["avg"] = 0.0
            else:
                total_cost = avg_price * position_qty + fill.price * signed_qty
                symbol_state["qty"] = new_qty
                symbol_state["avg"] = total_cost / new_qty
        else:
            close_qty = min(abs(position_qty), abs(signed_qty))
            direction = 1.0 if position_qty > 0 else -1.0
            realized += (fill.price - avg_price) * close_qty * direction
            new_qty = position_qty + signed_qty
            if new_qty == 0.0:
                symbol_state["qty"] = 0.0
                symbol_state["avg"] = 0.0
            elif position_qty * new_qty > 0:
                symbol_state["qty"] = new_qty
                symbol_state["avg"] = avg_price
            else:
                symbol_state["qty"] = new_qty
                symbol_state["avg"] = fill.price
        realized -= fill.fee
    return realized


def compute_unrealized_pnl(positions: Iterable[Position], marks: Mapping[str, float]) -> float:
    unrealized = 0.0
    for position in positions:
        qty = position.qty
        if qty == 0.0:
            continue
        mark = marks.get(position.symbol)
        if mark is None:
            continue
        unrealized += (mark - position.avg_entry) * qty
    return unrealized
