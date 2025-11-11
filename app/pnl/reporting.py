"""Lightweight helpers for producing daily PnL reports."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict

from .ledger import PnLLedger


def _to_string(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    try:
        return format(Decimal(value), "f")
    except (InvalidOperation, TypeError, ValueError):
        return str(value)


def make_daily_report(ledger: PnLLedger, as_of: datetime) -> Dict[str, Any]:
    """Return a structured snapshot suitable for JSON serialisation."""

    snapshot = ledger.get_snapshot()
    by_symbol: Dict[str, Dict[str, str]] = {}
    for venue, symbols in snapshot.get("by_venue", {}).items():
        for symbol, values in symbols.items():
            key = f"{venue}:{symbol}"
            fees_net = values.get("fees", Decimal("0")) - values.get("rebates", Decimal("0"))
            by_symbol[key] = {
                "realized_pnl": _to_string(values.get("realized_pnl", Decimal("0"))),
                "fees": _to_string(fees_net),
                "funding": _to_string(values.get("funding", Decimal("0"))),
                "net_pnl": _to_string(values.get("net_pnl", Decimal("0"))),
            }
    totals = snapshot.get("totals", {})
    totals_payload = {
        "realized_pnl": _to_string(totals.get("realized_pnl", Decimal("0"))),
        "fees": _to_string(totals.get("fees", Decimal("0")) - totals.get("rebates", Decimal("0"))),
        "funding": _to_string(totals.get("funding", Decimal("0"))),
        "net_pnl": _to_string(totals.get("net_pnl", Decimal("0"))),
    }
    return {
        "as_of": as_of.replace(microsecond=0).isoformat(),
        "symbols": by_symbol,
        "totals": totals_payload,
    }
