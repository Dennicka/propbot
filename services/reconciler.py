"""Detect and persist reconciliation gaps between local state and live exchange data."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from app import ledger
from app.services import runtime
from positions import list_positions


_ALERTS_PATH = Path("data/reconciliation_alerts.json")
_MAX_ALERTS = 250
_RELATIVE_TOLERANCE = 0.01  # 1 % deviation allowed before flagging size mismatch


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_symbol(value: Any) -> str:
    return str(value or "").upper()


def _normalise_venue(value: Any) -> str:
    return str(value or "").lower()


def _normalise_side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"long", "buy"}:
        return "long"
    if text in {"short", "sell"}:
        return "short"
    return ""


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _position_target_size(position: Mapping[str, Any], side: str) -> float:
    base = _as_float(position.get("base_size"))
    if base > 0:
        return abs(base)
    notional = _as_float(position.get("notional_usdt"))
    if notional <= 0:
        return 0.0
    if side == "short":
        entry_price = _as_float(position.get("entry_short_price"))
    else:
        entry_price = _as_float(position.get("entry_long_price"))
    if entry_price <= 0:
        entry_price = _as_float(position.get("entry_price"))
    if entry_price <= 0:
        return 0.0
    try:
        return abs(notional / entry_price)
    except ZeroDivisionError:
        return 0.0


def _leg_size(leg: Mapping[str, Any], position: Mapping[str, Any], side: str) -> float:
    for key in ("base_size", "filled_qty", "qty", "size"):
        value = _as_float(leg.get(key))
        if abs(value) > 0:
            return abs(value)
    notional = _as_float(leg.get("notional_usdt"))
    if notional <= 0:
        notional = _as_float(position.get("notional_usdt"))
    entry_price = _as_float(leg.get("entry_price"))
    if entry_price <= 0:
        if side == "short":
            entry_price = _as_float(position.get("entry_short_price"))
        else:
            entry_price = _as_float(position.get("entry_long_price"))
    if entry_price > 0 and notional > 0:
        try:
            return abs(notional / entry_price)
        except ZeroDivisionError:
            return 0.0
    base = _as_float(position.get("base_size"))
    return abs(base) if base else 0.0


def _expected_exposures(
    stored_positions: Sequence[Mapping[str, Any]]
) -> Tuple[Dict[Tuple[str, str, str], Dict[str, Any]], List[Dict[str, Any]]]:
    expected: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    partial_legs: List[Dict[str, Any]] = []
    for position in stored_positions:
        if not isinstance(position, Mapping):
            continue
        if bool(position.get("simulated")):
            continue
        status = str(position.get("status") or "").lower()
        legs = position.get("legs")
        if not isinstance(legs, Iterable):
            legs = []
        for leg in legs:
            if not isinstance(leg, Mapping):
                continue
            if bool(leg.get("simulated")):
                continue
            leg_status = str(leg.get("status") or status).lower()
            if leg_status in {"closed", "filled"}:
                continue
            leg_side = _normalise_side(leg.get("side"))
            venue = _normalise_venue(leg.get("venue") or position.get("long_venue") or position.get("short_venue"))
            if not leg_side:
                long_venue = _normalise_venue(position.get("long_venue"))
                short_venue = _normalise_venue(position.get("short_venue"))
                if venue == short_venue:
                    leg_side = "short"
                elif venue == long_venue:
                    leg_side = "long"
            if leg_side not in {"long", "short"}:
                continue
            symbol = _normalise_symbol(leg.get("symbol") or position.get("symbol"))
            if not venue or not symbol:
                continue
            key = (venue, symbol, leg_side)
            size = _leg_size(leg, position, leg_side)
            entry = expected.setdefault(
                key,
                {
                    "size": 0.0,
                    "positions": set(),
                    "statuses": set(),
                },
            )
            entry["size"] = float(entry["size"]) + size
            entry["positions"].add(str(position.get("id") or ""))
            entry["statuses"].add(leg_status or "unknown")
            if leg_status in {"partial", "missing"}:
                target_size = size or _position_target_size(position, leg_side)
                partial_legs.append(
                    {
                        "kind": "partial_leg_stalled",
                        "position_id": str(position.get("id") or ""),
                        "venue": venue,
                        "symbol": symbol,
                        "side": leg_side,
                        "leg_status": leg_status,
                        "expected_size": target_size,
                    }
                )
    for entry in expected.values():
        entry["positions"] = sorted(filter(None, entry["positions"]))
        entry["statuses"] = sorted(filter(None, entry["statuses"]))
    return expected, partial_legs


def _actual_exposures(exchange_positions: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str, str], float]:
    actual: Dict[Tuple[str, str, str], float] = {}
    for row in exchange_positions:
        if not isinstance(row, Mapping):
            continue
        venue = _normalise_venue(row.get("venue"))
        symbol = _normalise_symbol(row.get("symbol"))
        qty = _as_float(row.get("base_qty") or row.get("qty") or row.get("size"))
        if qty == 0:
            continue
        side_hint = _normalise_side(row.get("side"))
        side = side_hint or ("long" if qty > 0 else "short")
        if side not in {"long", "short"}:
            side = "long" if qty > 0 else "short"
        key = (venue, symbol, side)
        actual[key] = actual.get(key, 0.0) + abs(qty)
    return actual


def detect_desyncs(
    stored_positions: Sequence[Mapping[str, Any]],
    exchange_positions: Sequence[Mapping[str, Any]],
    open_orders: Sequence[Mapping[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    """Return reconciliation issues between local store and live exchange data."""

    expected_map, partial_legs = _expected_exposures(stored_positions)
    actual_map = _actual_exposures(exchange_positions)

    issues: List[Dict[str, Any]] = []
    tolerance = 1e-6

    for key, payload in expected_map.items():
        expected_size = float(payload.get("size", 0.0))
        actual_size = float(actual_map.get(key, 0.0))
        venue, symbol, side = key
        if actual_size <= tolerance:
            issues.append(
                {
                    "kind": "position_missing_on_exchange",
                    "venue": venue,
                    "symbol": symbol,
                    "side": side,
                    "expected_size": expected_size,
                    "actual_size": 0.0,
                    "positions": payload.get("positions", []),
                    "description": "Store marks position open but exchange reports none.",
                }
            )
            continue
        deviation = abs(expected_size - actual_size)
        allowed = max(tolerance, expected_size * _RELATIVE_TOLERANCE)
        if deviation > allowed:
            issues.append(
                {
                    "kind": "position_size_mismatch",
                    "venue": venue,
                    "symbol": symbol,
                    "side": side,
                    "expected_size": expected_size,
                    "actual_size": actual_size,
                    "positions": payload.get("positions", []),
                    "description": "Exchange exposure size diverges from store record.",
                }
            )

    for key, actual_size in actual_map.items():
        if key in expected_map:
            continue
        venue, symbol, side = key
        issues.append(
            {
                "kind": "unexpected_exchange_position",
                "venue": venue,
                "symbol": symbol,
                "side": side,
                "expected_size": 0.0,
                "actual_size": actual_size,
                "positions": [],
                "description": "Exchange shows exposure but store marks position closed.",
            }
        )

    issues.extend(partial_legs)

    return issues


def _load_alerts(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, list):
        result: List[Dict[str, Any]] = []
        for entry in data:
            if isinstance(entry, Mapping):
                result.append(dict(entry))
        return result
    return []


def _persist_alert(snapshot: Mapping[str, Any], *, path: Path = _ALERTS_PATH) -> None:
    records = _load_alerts(path)
    records.append(dict(snapshot))
    if len(records) > _MAX_ALERTS:
        records = records[-_MAX_ALERTS:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2, sort_keys=True), encoding="utf-8")


def reconcile(
    *,
    stored_positions: Sequence[Mapping[str, Any]] | None = None,
    exchange_positions: Sequence[Mapping[str, Any]] | None = None,
    open_orders: Sequence[Mapping[str, Any]] | None = None,
    alerts_path: Path = _ALERTS_PATH,
) -> List[Dict[str, Any]]:
    """Run reconciliation and persist alerts/desync flag if mismatches are detected."""

    stored = [dict(entry) for entry in (stored_positions or list_positions())]
    exchange = [dict(row) for row in (exchange_positions or ledger.fetch_positions())]
    orders = [dict(row) for row in (open_orders or ledger.fetch_open_orders())]

    issues = detect_desyncs(stored, exchange, orders)
    timestamp = _ts()

    metadata: Dict[str, Any] = {"open_orders_observed": len(orders)}
    runtime.update_reconciliation_status(
        desync_detected=bool(issues),
        issues=issues,
        last_checked=timestamp,
        metadata=metadata,
    )

    if issues:
        snapshot = {
            "timestamp": timestamp,
            "issue_count": len(issues),
            "issues": issues[:50],
        }
        _persist_alert(snapshot, path=alerts_path)

    return issues


__all__ = ["detect_desyncs", "reconcile"]

