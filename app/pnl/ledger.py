"""Decimal-first PnL ledger tracking realized PnL, fees and funding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterator, List, Mapping, MutableMapping, Tuple
from zoneinfo import ZoneInfo

from ..utils.decimal import decimal_context, to_decimal


@dataclass(frozen=True)
class TradeFill:
    venue: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal
    fee_asset: str
    ts: float
    is_simulated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "venue", str(self.venue or "").lower())
        object.__setattr__(self, "symbol", str(self.symbol or "").upper())
        object.__setattr__(self, "side", str(self.side or "").upper())
        object.__setattr__(self, "fee_asset", str(self.fee_asset or "").upper())
        object.__setattr__(self, "qty", to_decimal(self.qty, default=Decimal("0")))
        object.__setattr__(self, "price", to_decimal(self.price, default=Decimal("0")))
        object.__setattr__(self, "fee", to_decimal(self.fee, default=Decimal("0")))
        object.__setattr__(self, "ts", float(self.ts))


@dataclass(frozen=True)
class FundingEvent:
    venue: str
    symbol: str
    amount: Decimal
    asset: str
    ts: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "venue", str(self.venue or "").lower())
        object.__setattr__(self, "symbol", str(self.symbol or "").upper())
        object.__setattr__(self, "asset", str(self.asset or "").upper())
        object.__setattr__(self, "amount", to_decimal(self.amount, default=Decimal("0")))
        object.__setattr__(self, "ts", float(self.ts))


@dataclass
class LedgerEntry:
    ts: float
    venue: str
    symbol: str
    realized_pnl: Decimal
    fee: Decimal
    funding: Decimal
    rebate: Decimal
    notional: Decimal
    side: str
    is_simulated: bool


@dataclass
class DailyPnLSnapshot:
    date: str
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    rebates: Decimal
    net_pnl: Decimal


@dataclass
class _PositionState:
    qty: Decimal = Decimal("0")
    avg_price: Decimal = Decimal("0")


@dataclass
class _Totals:
    realized: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")
    rebates: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")

    @property
    def net(self) -> Decimal:
        return self.realized - self.fees + self.funding + self.rebates


def _normalise_side(side: str) -> str:
    text = (side or "").strip().upper()
    if text in {"BUY", "SELL"}:
        return text
    if text in {"BID", "LONG"}:
        return "BUY"
    if text in {"ASK", "SHORT"}:
        return "SELL"
    return "BUY"


def _date_key(ts: float, tz: str | ZoneInfo) -> str:
    tzinfo = ZoneInfo(tz) if isinstance(tz, str) else tz
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tzinfo)
    return dt.date().isoformat()


class PnLLedger:
    """Ledger computing realised PnL, fees and funding using :class:`Decimal`."""

    def __init__(self) -> None:
        self._positions: Dict[Tuple[str, str], _PositionState] = {}
        self._totals: Dict[Tuple[str, str], _Totals] = {}
        self._events: List[LedgerEntry] = []

    def apply_fill(self, fill: TradeFill, *, exclude_simulated: bool) -> None:
        if fill.is_simulated and exclude_simulated:
            return
        key = (fill.venue, fill.symbol)
        state = self._positions.setdefault(key, _PositionState())
        totals = self._totals.setdefault(key, _Totals())
        with decimal_context():
            qty = fill.qty.copy_abs()
            signed_qty = qty if _normalise_side(fill.side) == "BUY" else -qty
            notional = (fill.price * qty).copy_abs()
            realized_delta = Decimal("0")
            if qty != 0 and fill.price != 0:
                if state.qty == 0 or state.qty * signed_qty > 0:
                    new_qty = state.qty + signed_qty
                    if new_qty == 0:
                        state.qty = Decimal("0")
                        state.avg_price = Decimal("0")
                    else:
                        total_cost = state.avg_price * state.qty + fill.price * signed_qty
                        state.qty = new_qty
                        state.avg_price = total_cost / new_qty
                else:
                    close_qty = min(abs(state.qty), abs(signed_qty))
                    direction = Decimal("1") if state.qty > 0 else Decimal("-1")
                    realized_delta = (fill.price - state.avg_price) * close_qty * direction
                    remaining_qty = state.qty + signed_qty
                    if remaining_qty == 0:
                        state.qty = Decimal("0")
                        state.avg_price = Decimal("0")
                    elif state.qty * remaining_qty > 0:
                        state.qty = remaining_qty
                    else:
                        state.qty = remaining_qty
                        state.avg_price = fill.price
            else:
                notional = Decimal("0")
        fee = fill.fee
        rebate = Decimal("0")
        if fee < 0:
            rebate = -fee
            fee = Decimal("0")
        entry = LedgerEntry(
            ts=fill.ts,
            venue=fill.venue,
            symbol=fill.symbol,
            realized_pnl=realized_delta,
            fee=fee,
            funding=Decimal("0"),
            rebate=rebate,
            notional=notional,
            side=_normalise_side(fill.side),
            is_simulated=fill.is_simulated,
        )
        self._events.append(entry)
        totals.realized += realized_delta
        totals.fees += fee
        totals.rebates += rebate

    def apply_funding(self, event: FundingEvent) -> None:
        key = (event.venue, event.symbol)
        totals = self._totals.setdefault(key, _Totals())
        entry = LedgerEntry(
            ts=event.ts,
            venue=event.venue,
            symbol=event.symbol,
            realized_pnl=Decimal("0"),
            fee=Decimal("0"),
            funding=event.amount,
            rebate=Decimal("0"),
            notional=Decimal("0"),
            side="BOTH",
            is_simulated=False,
        )
        self._events.append(entry)
        totals.funding += event.amount

    def iter_entries(self) -> Iterator[LedgerEntry]:
        yield from sorted(self._events, key=lambda entry: entry.ts)

    def get_snapshot(self) -> Mapping[str, object]:
        per_symbol: Dict[str, Dict[str, object]] = {}
        total_realized = Decimal("0")
        total_fees = Decimal("0")
        total_rebates = Decimal("0")
        total_funding = Decimal("0")
        for (venue, symbol), totals in self._totals.items():
            venue_bucket = per_symbol.setdefault(venue, {})
            venue_bucket[symbol] = {
                "realized_pnl": totals.realized,
                "fees": totals.fees,
                "rebates": totals.rebates,
                "funding": totals.funding,
                "net_pnl": totals.net,
            }
            total_realized += totals.realized
            total_fees += totals.fees
            total_rebates += totals.rebates
            total_funding += totals.funding
        return {
            "by_venue": per_symbol,
            "totals": {
                "realized_pnl": total_realized,
                "fees": total_fees,
                "rebates": total_rebates,
                "funding": total_funding,
                "net_pnl": total_realized - total_fees + total_funding + total_rebates,
            },
        }

    def daily_snapshots(self, tz: str | ZoneInfo = "UTC") -> List[DailyPnLSnapshot]:
        buckets: MutableMapping[str, _Totals] = {}
        for entry in self._events:
            date_key = _date_key(entry.ts, tz)
            bucket = buckets.setdefault(date_key, _Totals())
            bucket.realized += entry.realized_pnl
            bucket.fees += entry.fee
            bucket.rebates += entry.rebate
            bucket.funding += entry.funding
        snapshots: List[DailyPnLSnapshot] = []
        for date_key in sorted(buckets.keys()):
            bucket = buckets[date_key]
            snapshots.append(
                DailyPnLSnapshot(
                    date=date_key,
                    realized_pnl=bucket.realized,
                    fees=bucket.fees,
                    funding=bucket.funding,
                    rebates=bucket.rebates,
                    net_pnl=bucket.net,
                )
            )
        return snapshots


__all__ = [
    "DailyPnLSnapshot",
    "FundingEvent",
    "LedgerEntry",
    "PnLLedger",
    "TradeFill",
]
