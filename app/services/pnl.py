from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from app.pnl.ledger import PnLLedger, TradeFill
from app.utils.decimal import to_decimal


@dataclass(frozen=True)
class RealizedPnLBreakdown:
    """Breakdown of realised trading PnL and associated fees."""

    trading: float = 0.0
    fees: float = 0.0

    @property
    def net(self) -> float:
        """Return realised PnL net of fees."""

        return self.trading - self.fees


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


def compute_realized_breakdown(fills: Sequence[Fill]) -> RealizedPnLBreakdown:
    """Compute realised trading PnL and fees for ``fills``."""
    ledger = _build_ledger_from_fills(fills)
    realized, fees = _ledger_totals(ledger)
    return RealizedPnLBreakdown(trading=realized, fees=fees)


def compute_realized_breakdown_by_symbol(
    fills: Sequence[Fill],
) -> dict[str, RealizedPnLBreakdown]:
    """Return realised PnL breakdown grouped by symbol."""

    ledger = _build_ledger_from_fills(fills)
    snapshot = ledger.get_snapshot()
    breakdowns: dict[str, RealizedPnLBreakdown] = {}
    for venue_payload in snapshot.get("by_venue", {}).values():
        for symbol, values in venue_payload.items():
            trading = float(values.get("realized_pnl", 0.0))
            fees = float(values.get("fees", 0.0) - values.get("rebates", 0.0))
            entry = breakdowns.get(symbol)
            if entry is None:
                breakdowns[symbol] = RealizedPnLBreakdown(trading=trading, fees=fees)
            else:
                breakdowns[symbol] = RealizedPnLBreakdown(
                    trading=entry.trading + trading,
                    fees=entry.fees + fees,
                )
    return breakdowns


def compute_realized_pnl(fills: Sequence[Fill]) -> float:
    return compute_realized_breakdown(fills).net


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


def _build_ledger_from_fills(fills: Sequence[Fill]) -> PnLLedger:
    ledger = PnLLedger()
    for fill in sorted(fills, key=_sort_key):
        if fill.qty <= 0:
            continue
        trade = _fill_to_trade_fill(fill)
        ledger.apply_fill(trade, exclude_simulated=False)
    return ledger


def _ledger_totals(ledger: PnLLedger) -> tuple[float, float]:
    snapshot = ledger.get_snapshot()
    totals = snapshot.get("totals", {})
    realized = float(totals.get("realized_pnl", 0.0))
    fees = float(totals.get("fees", 0.0) - totals.get("rebates", 0.0))
    return realized, fees


def _fill_to_trade_fill(fill: Fill) -> TradeFill:
    qty = to_decimal(fill.qty, default=Decimal("0")).copy_abs()
    price = to_decimal(fill.price, default=Decimal("0"))
    fee = to_decimal(fill.fee, default=Decimal("0"))
    side = "BUY" if fill.side in {"buy", "long", "bid"} else "SELL"
    ts = fill.ts.timestamp() if fill.ts else 0.0
    symbol = (fill.symbol or "").upper() or "UNKNOWN"
    return TradeFill(
        venue="internal",
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        fee_asset="USD",
        ts=ts,
        is_simulated=False,
    )
