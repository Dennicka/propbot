"""Simple storage for cross-exchange hedge positions."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

from app.services import runtime


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
}


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_positions() -> List[Dict[str, Any]]:
    """Return all recorded hedge positions (both open and closed)."""

    return runtime.get_positions_state()


def list_open_positions() -> List[Dict[str, Any]]:
    """Return only open hedge positions."""

    return [entry for entry in list_positions() if entry.get("status") == "open"]


def _with_defaults(record: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(record)
    payload.setdefault("pnl_usdt", 0.0)
    payload.setdefault("status", "open")
    payload.setdefault("timestamp", _ts())
    payload.setdefault("id", uuid.uuid4().hex)
    return payload


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
    if entry_long_price not in (None, 0) and notional_usdt:
        try:
            payload["base_size"] = float(notional_usdt) / float(entry_long_price)
        except ZeroDivisionError:
            payload["base_size"] = 0.0
    payload = _with_defaults(payload)
    runtime.append_position_state(payload)
    return payload


def close_position(
    position_id: str,
    *,
    exit_long_price: float,
    exit_short_price: float,
) -> Dict[str, Any]:
    """Close an existing position and compute realized PnL."""

    positions = runtime.get_positions_state()
    updated: Dict[str, Any] | None = None
    for entry in positions:
        if str(entry.get("id")) != str(position_id):
            continue
        if entry.get("status") == "closed":
            updated = entry
            break
        base_size = float(entry.get("base_size") or 0.0)
        if base_size == 0.0:
            long_price = float(entry.get("entry_long_price") or exit_long_price or 1.0)
            if long_price:
                base_size = float(entry.get("notional_usdt", 0.0)) / float(long_price)
        pnl = (float(exit_short_price) - float(exit_long_price)) * base_size
        entry.update(
            {
                "status": "closed",
                "pnl_usdt": float(pnl),
                "exit_long_price": float(exit_long_price),
                "exit_short_price": float(exit_short_price),
                "closed_ts": _ts(),
            }
        )
        updated = entry
        break
    if updated is None:
        raise KeyError(f"position {position_id} not found")
    runtime.set_positions_state(positions)
    return updated


def reset_positions() -> None:
    """Clear all stored positions (used in tests)."""

    runtime.set_positions_state([])


def validate_record_structure(entries: Iterable[Dict[str, Any]]) -> None:
    for entry in entries:
        missing = _REQUIRED_FIELDS - set(entry)
        if missing:
            raise ValueError(f"position record missing fields: {', '.join(sorted(missing))}")
