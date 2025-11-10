from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Iterator, List, Sequence
from zoneinfo import ZoneInfo


def _to_decimal(value: object, *, default: Decimal = Decimal("0")) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation:
            return default
    return default


def _normalise_side(value: str | None) -> str:
    if not value:
        return "FLAT"
    text = value.strip().upper()
    if text in {"LONG", "SHORT", "FLAT", "BOTH"}:
        return text
    if text in {"BUY", "BID"}:
        return "LONG"
    if text in {"SELL", "ASK"}:
        return "SHORT"
    return "FLAT"


def _date_key(ts: float, tz: str | ZoneInfo) -> str:
    if isinstance(tz, str):
        try:
            tzinfo = ZoneInfo(tz)
        except Exception:  # pragma: no cover - defensive fallback
            tzinfo = timezone.utc
    else:
        tzinfo = tz
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tzinfo)
    return dt.date().isoformat()


@dataclass
class PnLEntry:
    ts: float
    symbol: str
    side: str
    realized_pnl: Decimal
    fee: Decimal
    funding: Decimal
    rebate: Decimal
    notional: Decimal
    kind: str
    ref_id: str | None


@dataclass
class DailyPnlSnapshot:
    date: str
    realized_pnl: Decimal
    fees: Decimal
    funding: Decimal
    rebates: Decimal
    net_pnl: Decimal
    opening_balance: Decimal | None
    closing_balance: Decimal | None


class PnLLedger:
    """In-memory ledger tracking realized PnL, fees and adjustments."""

    def __init__(self, entries: Iterable[PnLEntry] | None = None) -> None:
        self._entries: List[PnLEntry] = []
        self._metadata: List[dict[str, object]] = []
        if entries:
            for entry in entries:
                self._append(entry, metadata={})

    def __len__(self) -> int:
        return len(self._entries)

    def _append(self, entry: PnLEntry, metadata: dict[str, object]) -> None:
        self._entries.append(entry)
        self._metadata.append(dict(metadata))

    def record_fill(
        self,
        *,
        ts: float,
        symbol: str,
        side: str,
        realized_pnl: object,
        fee: object,
        notional: object,
        ref_id: str | None = None,
        rebate: object = Decimal("0"),
        funding: object = Decimal("0"),
        simulated: bool = False,
    ) -> PnLEntry:
        entry = PnLEntry(
            ts=float(ts),
            symbol=str(symbol or "").upper(),
            side=_normalise_side(side),
            realized_pnl=_to_decimal(realized_pnl),
            fee=_to_decimal(fee),
            funding=_to_decimal(funding),
            rebate=_to_decimal(rebate),
            notional=_to_decimal(notional).copy_abs(),
            kind="FILL",
            ref_id=str(ref_id) if ref_id is not None else None,
        )
        metadata = {"simulated": bool(simulated)}
        self._append(entry, metadata)
        setattr(entry, "simulated", bool(simulated))
        return entry

    def record_funding(
        self,
        *,
        ts: float,
        symbol: str,
        amount: object,
        ref_id: str | None = None,
        side: str = "BOTH",
        simulated: bool = False,
    ) -> PnLEntry:
        entry = PnLEntry(
            ts=float(ts),
            symbol=str(symbol or "").upper(),
            side=_normalise_side(side),
            realized_pnl=Decimal("0"),
            fee=Decimal("0"),
            funding=_to_decimal(amount),
            rebate=Decimal("0"),
            notional=Decimal("0"),
            kind="FUNDING",
            ref_id=str(ref_id) if ref_id is not None else None,
        )
        metadata = {"simulated": bool(simulated)}
        self._append(entry, metadata)
        setattr(entry, "simulated", bool(simulated))
        return entry

    def record_adjustment(
        self,
        *,
        ts: float,
        symbol: str,
        realized_pnl: object = Decimal("0"),
        fee: object = Decimal("0"),
        funding: object = Decimal("0"),
        rebate: object = Decimal("0"),
        notional: object = Decimal("0"),
        ref_id: str | None = None,
        side: str = "FLAT",
        simulated: bool = False,
    ) -> PnLEntry:
        entry = PnLEntry(
            ts=float(ts),
            symbol=str(symbol or "").upper(),
            side=_normalise_side(side),
            realized_pnl=_to_decimal(realized_pnl),
            fee=_to_decimal(fee),
            funding=_to_decimal(funding),
            rebate=_to_decimal(rebate),
            notional=_to_decimal(notional).copy_abs(),
            kind="ADJUSTMENT",
            ref_id=str(ref_id) if ref_id is not None else None,
        )
        metadata = {"simulated": bool(simulated)}
        self._append(entry, metadata)
        setattr(entry, "simulated", bool(simulated))
        return entry

    def iter_entries(self) -> Iterator[PnLEntry]:
        ordered = sorted(zip(self._entries, self._metadata), key=lambda item: item[0].ts)
        for entry, metadata in ordered:
            setattr(entry, "simulated", bool(metadata.get("simulated", False)))
            yield entry

    def daily_snapshots(self, tz: str | ZoneInfo = "UTC") -> List[DailyPnlSnapshot]:
        if not self._entries:
            return []
        buckets: dict[str, dict[str, Decimal]] = {}
        ordered_entries = list(self.iter_entries())
        for entry in ordered_entries:
            date_key = _date_key(entry.ts, tz)
            bucket = buckets.setdefault(
                date_key,
                {
                    "realized": Decimal("0"),
                    "fees": Decimal("0"),
                    "funding": Decimal("0"),
                    "rebates": Decimal("0"),
                },
            )
            bucket["realized"] += entry.realized_pnl
            bucket["fees"] += entry.fee
            bucket["funding"] += entry.funding
            bucket["rebates"] += entry.rebate

        snapshots: List[DailyPnlSnapshot] = []
        closing_balance = Decimal("0")
        for index, date_key in enumerate(sorted(buckets.keys())):
            bucket = buckets[date_key]
            realized = bucket["realized"]
            fees = bucket["fees"]
            funding = bucket["funding"]
            rebates = bucket["rebates"]
            net = realized - fees + funding + rebates
            opening = closing_balance if index > 0 else None
            closing = (opening if opening is not None else Decimal("0")) + net
            snapshot = DailyPnlSnapshot(
                date=date_key,
                realized_pnl=realized,
                fees=fees,
                funding=funding,
                rebates=rebates,
                net_pnl=net,
                opening_balance=opening,
                closing_balance=closing,
            )
            snapshots.append(snapshot)
            closing_balance = closing
        return snapshots


__all__: Sequence[str] = [
    "PnLEntry",
    "DailyPnlSnapshot",
    "PnLLedger",
]
