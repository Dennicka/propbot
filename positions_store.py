"""Durable storage for cross-exchange hedge positions."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping


_STORE_ENV = "POSITIONS_STORE_PATH"
_DEFAULT_PATH = Path("data/hedge_positions.json")
_LOCK = threading.RLock()


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store_path() -> Path:
    override = os.environ.get(_STORE_ENV)
    if override:
        return Path(override)
    return _DEFAULT_PATH


def get_store_path() -> Path:
    """Return the configured path for the hedge positions store."""

    return _store_path()


def _ensure_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _load_entries(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    entries: List[Dict[str, Any]] = []
    for row in payload:
        if isinstance(row, Mapping):
            entries.append({str(key): value for key, value in row.items()})
    return entries


def _write_entries(path: Path, entries: Iterable[Mapping[str, Any]]) -> None:
    snapshot = [dict(row) for row in entries]
    _ensure_parent(path)
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, sort_keys=True)
    except OSError:
        pass


def list_records() -> List[Dict[str, Any]]:
    """Return all hedge position records from disk."""

    path = _store_path()
    with _LOCK:
        entries = _load_entries(path)
    return [dict(entry) for entry in entries]


def _normalise_leg(
    *,
    venue: str,
    symbol: str,
    side: str,
    notional_usdt: float,
    entry_price: float | None,
    leverage: float | None,
    timestamp: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "venue": str(venue),
        "symbol": str(symbol).upper(),
        "side": str(side).lower(),
        "notional_usdt": float(notional_usdt),
        "entry_price": float(entry_price) if entry_price not in (None, "") else None,
        "leverage": float(leverage) if leverage not in (None, "") else None,
        "timestamp": timestamp,
    }
    entry_price_value = payload.get("entry_price")
    if entry_price_value:
        try:
            payload["base_size"] = payload["notional_usdt"] / float(entry_price_value)
        except ZeroDivisionError:
            payload["base_size"] = 0.0
    else:
        payload["base_size"] = 0.0
    return payload


def _prepare_record(payload: Mapping[str, Any]) -> Dict[str, Any]:
    timestamp = str(payload.get("timestamp") or _ts())
    long_venue = str(payload.get("long_venue") or "")
    short_venue = str(payload.get("short_venue") or "")
    symbol = str(payload.get("symbol") or "").upper()
    notional_usdt = float(payload.get("notional_usdt") or 0.0)
    entry_long_price = payload.get("entry_long_price")
    entry_short_price = payload.get("entry_short_price")
    leverage = payload.get("leverage")
    try:
        base_size = notional_usdt / float(entry_long_price)
    except (TypeError, ValueError, ZeroDivisionError):
        base_size = 0.0
    record: Dict[str, Any] = {
        "id": str(payload.get("id") or uuid.uuid4().hex),
        "timestamp": timestamp,
        "status": str(payload.get("status") or "open"),
        "symbol": symbol,
        "long_venue": long_venue,
        "short_venue": short_venue,
        "notional_usdt": notional_usdt,
        "entry_spread_bps": float(payload.get("entry_spread_bps") or 0.0),
        "leverage": float(leverage) if leverage not in (None, "") else None,
        "entry_long_price": float(entry_long_price) if entry_long_price not in (None, "") else None,
        "entry_short_price": float(entry_short_price) if entry_short_price not in (None, "") else None,
        "pnl_usdt": float(payload.get("pnl_usdt") or 0.0),
        "base_size": float(base_size),
    }
    if payload.get("simulated") is not None:
        record["simulated"] = bool(payload.get("simulated"))
    record["legs"] = [
        _normalise_leg(
            venue=long_venue,
            symbol=symbol,
            side="long",
            notional_usdt=notional_usdt,
            entry_price=record["entry_long_price"],
            leverage=record["leverage"],
            timestamp=timestamp,
        ),
        _normalise_leg(
            venue=short_venue,
            symbol=symbol,
            side="short",
            notional_usdt=notional_usdt,
            entry_price=record["entry_short_price"],
            leverage=record["leverage"],
            timestamp=timestamp,
        ),
    ]
    return record


def append_record(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Append a new hedge position to the persistent store."""

    path = _store_path()
    record = _prepare_record(payload)
    with _LOCK:
        entries = _load_entries(path)
        entries.append(record)
        _write_entries(path, entries)
    return dict(record)


def _locate_entry(entries: List[MutableMapping[str, Any]], position_id: str) -> MutableMapping[str, Any] | None:
    for entry in entries:
        if str(entry.get("id")) == str(position_id):
            return entry
    return None


def mark_closed(
    position_id: str,
    *,
    exit_long_price: float,
    exit_short_price: float,
) -> Dict[str, Any]:
    """Mark an existing position as closed and persist the update."""

    path = _store_path()
    with _LOCK:
        entries = _load_entries(path)
        target = _locate_entry(entries, position_id)
        if target is None:
            raise KeyError(f"position {position_id} not found")
        if str(target.get("status")) == "closed":
            return dict(target)
        long_leg, short_leg = target.get("legs", [None, None])[:2]
        base_size = float(target.get("base_size") or 0.0)
        entry_long = float(target.get("entry_long_price") or exit_long_price or 0.0)
        if base_size <= 0.0 and entry_long:
            try:
                base_size = float(target.get("notional_usdt", 0.0)) / entry_long
            except ZeroDivisionError:
                base_size = 0.0
        long_qty = float(long_leg.get("base_size")) if isinstance(long_leg, Mapping) else base_size
        short_qty = float(short_leg.get("base_size")) if isinstance(short_leg, Mapping) else base_size
        quantity = long_qty or short_qty or base_size
        pnl = (float(exit_short_price) - float(exit_long_price)) * float(quantity)
        target.update(
            {
                "status": "closed",
                "pnl_usdt": float(pnl),
                "exit_long_price": float(exit_long_price),
                "exit_short_price": float(exit_short_price),
                "closed_ts": _ts(),
            }
        )
        if isinstance(long_leg, MutableMapping):
            long_leg.update(
                {
                    "exit_price": float(exit_long_price),
                    "closed_ts": target["closed_ts"],
                }
            )
        if isinstance(short_leg, MutableMapping):
            short_leg.update(
                {
                    "exit_price": float(exit_short_price),
                    "closed_ts": target["closed_ts"],
                }
            )
        _write_entries(path, entries)
        return dict(target)


def reset_store() -> None:
    """Reset the hedge positions store (used in tests)."""

    path = _store_path()
    with _LOCK:
        _write_entries(path, [])


__all__ = [
    "append_record",
    "get_store_path",
    "list_records",
    "mark_closed",
    "reset_store",
]

