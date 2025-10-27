"""Simple storage for cross-exchange hedge positions."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from app.services import runtime
from positions_store import append_record, list_records, mark_closed, reset_store


_REQUIRED_FIELDS = {
    "id",
    "timestamp",
    "long_venue",
    "short_venue",
    "symbol",
    "notional_usdt",
    "entry_spread_bps",
    "leverage",
    "status",
    "pnl_usdt",
    "legs",
}


def list_positions() -> List[Dict[str, Any]]:
    """Return all recorded hedge positions (both open and closed)."""

    return list_records()


def list_open_positions() -> List[Dict[str, Any]]:
    """Return only open hedge positions."""

    open_statuses = {"open", "simulated"}
    return [
        entry
        for entry in list_positions()
        if str(entry.get("status", "")).lower() in open_statuses
    ]


def create_position(
    *,
    symbol: str,
    long_venue: str,
    short_venue: str,
    notional_usdt: float,
    entry_spread_bps: float,
    leverage: float,
    entry_long_price: float | None = None,
    entry_short_price: float | None = None,
    status: str | None = None,
    simulated: bool | None = None,
) -> Dict[str, Any]:
    """Create and persist a new hedge position."""

    payload: Dict[str, Any] = {
        "symbol": symbol,
        "long_venue": long_venue,
        "short_venue": short_venue,
        "notional_usdt": float(notional_usdt),
        "entry_spread_bps": float(entry_spread_bps),
        "leverage": float(leverage),
        "entry_long_price": float(entry_long_price)
        if entry_long_price is not None
        else None,
        "entry_short_price": float(entry_short_price)
        if entry_short_price is not None
        else None,
    }
    if status:
        payload["status"] = str(status)
    if simulated is not None:
        payload["simulated"] = bool(simulated)
    if entry_long_price not in (None, 0) and notional_usdt:
        try:
            payload["base_size"] = float(notional_usdt) / float(entry_long_price)
        except ZeroDivisionError:
            payload["base_size"] = 0.0
    record = append_record(payload)
    runtime.append_position_state(record)
    return record


def close_position(
    position_id: str,
    *,
    exit_long_price: float,
    exit_short_price: float,
) -> Dict[str, Any]:
    """Close an existing position and compute realized PnL."""

    updated = mark_closed(
        position_id,
        exit_long_price=float(exit_long_price),
        exit_short_price=float(exit_short_price),
    )
    runtime.set_positions_state(list_records())
    return updated


def reset_positions() -> None:
    """Clear all stored positions (used in tests)."""

    reset_store()
    runtime.set_positions_state([])


def validate_record_structure(entries: Iterable[Dict[str, Any]]) -> None:
    for entry in entries:
        missing = _REQUIRED_FIELDS - set(entry)
        if missing:
            raise ValueError(f"position record missing fields: {', '.join(sorted(missing))}")
        legs = entry.get("legs")
        if not isinstance(legs, list) or len(legs) != 2:
            raise ValueError("position record missing leg metadata")
        for leg in legs:
            if not isinstance(leg, dict):
                raise ValueError("invalid leg payload")
            required_leg = {"venue", "symbol", "side", "notional_usdt", "timestamp"}
            missing_leg = required_leg - set(leg)
            if missing_leg:
                raise ValueError(f"position leg missing fields: {', '.join(sorted(missing_leg))}")
