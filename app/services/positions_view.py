from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Tuple


async def build_positions_snapshot(
    state, positions: Iterable[Mapping[str, Any]]
) -> Dict[str, Any]:
    marks = await _resolve_position_marks(state, positions)
    exposure_totals: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"long_notional": 0.0, "short_notional": 0.0, "net_usdt": 0.0}
    )
    enriched: List[Dict[str, Any]] = []
    aggregate_upnl = 0.0
    for entry in positions:
        base = dict(entry)
        legs_payload = _normalise_legs(entry.get("legs"), base)
        status = str(base.get("status") or "").lower()
        is_open = status in {"open", "partial"}
        pair_upnl = 0.0
        rendered_legs: List[Dict[str, Any]] = []
        for leg in legs_payload:
            venue = str(leg.get("venue") or "")
            symbol = str(leg.get("symbol") or "").upper()
            side = str(leg.get("side") or "").lower()
            try:
                notional = float(leg.get("notional_usdt") or 0.0)
            except (TypeError, ValueError):
                notional = 0.0
            try:
                entry_price = float(leg.get("entry_price") or 0.0)
            except (TypeError, ValueError):
                entry_price = 0.0
            try:
                base_size = float(leg.get("base_size") or 0.0)
            except (TypeError, ValueError):
                base_size = 0.0
            if base_size <= 0.0 and entry_price:
                try:
                    base_size = notional / entry_price
                except ZeroDivisionError:
                    base_size = 0.0
            mark_price = marks.get((venue, symbol))
            if mark_price is None:
                mark_price = entry_price
            pnl = 0.0
            if base_size and entry_price and mark_price is not None:
                if side == "short":
                    pnl = (entry_price - mark_price) * base_size
                else:
                    pnl = (mark_price - entry_price) * base_size
            if is_open:
                pair_upnl += pnl
                exposure_entry = exposure_totals[venue]
                if side == "short":
                    exposure_entry["short_notional"] += notional
                    exposure_entry["net_usdt"] -= notional
                else:
                    exposure_entry["long_notional"] += notional
                    exposure_entry["net_usdt"] += notional
            status_value = leg.get("status") or base.get("status")
            if status_value in (None, "") and is_open:
                status_value = "open"
            rendered_legs.append(
                {
                    "venue": venue,
                    "symbol": symbol,
                    "side": side,
                    "status": str(status_value or "").lower(),
                    "notional_usdt": notional,
                    "entry_price": entry_price,
                    "mark_price": mark_price,
                    "base_size": base_size,
                    "unrealized_pnl_usdt": pnl if is_open else 0.0,
                    "timestamp": leg.get("timestamp"),
                }
            )
        if is_open:
            aggregate_upnl += pair_upnl
        base["legs"] = rendered_legs
        base["unrealized_pnl_usdt"] = pair_upnl if is_open else 0.0
        enriched.append(base)
    exposure = {
        venue: {
            "long_notional": round(values["long_notional"], 6),
            "short_notional": round(values["short_notional"], 6),
            "net_usdt": round(values["net_usdt"], 6),
        }
        for venue, values in exposure_totals.items()
        if venue
    }
    return {
        "positions": enriched,
        "exposure": exposure,
        "totals": {"unrealized_pnl_usdt": aggregate_upnl},
    }


def _normalise_legs(payload: object, base: Mapping[str, Any]) -> List[Dict[str, Any]]:
    legs: List[Dict[str, Any]] = []
    if isinstance(payload, Iterable):
        for index, leg in enumerate(payload):
            if not isinstance(leg, Mapping):
                continue
            default_side = "long" if index == 0 else "short"
            legs.append(_build_leg_payload(leg, base, default_side))
    if len(legs) >= 2:
        return legs[:2]
    return [
        _build_leg_payload({}, base, "long"),
        _build_leg_payload({}, base, "short"),
    ]


def _build_leg_payload(
    leg: Mapping[str, Any],
    base: Mapping[str, Any],
    default_side: str,
) -> Dict[str, Any]:
    side = str(leg.get("side") or default_side).lower()
    venue_key = "long_venue" if side != "short" else "short_venue"
    venue = str(leg.get("venue") or base.get(venue_key) or "")
    symbol = str(leg.get("symbol") or base.get("symbol") or "").upper()
    notional_raw = leg.get("notional_usdt")
    if notional_raw in (None, ""):
        notional_raw = base.get("notional_usdt")
    try:
        notional = float(notional_raw or 0.0)
    except (TypeError, ValueError):
        notional = 0.0
    entry_key = "entry_long_price" if side != "short" else "entry_short_price"
    entry_raw = leg.get("entry_price", base.get(entry_key))
    try:
        entry_price = float(entry_raw)
    except (TypeError, ValueError):
        entry_price = 0.0
    base_size_raw = leg.get("base_size")
    if base_size_raw in (None, ""):
        base_size_raw = base.get("base_size")
    try:
        base_size = float(base_size_raw or 0.0)
    except (TypeError, ValueError):
        base_size = 0.0
    if base_size <= 0.0 and entry_price:
        try:
            base_size = notional / entry_price
        except ZeroDivisionError:
            base_size = 0.0
    timestamp = leg.get("timestamp") or base.get("timestamp")
    status_value = leg.get("status") or base.get("status")
    return {
        "venue": venue,
        "symbol": symbol,
        "side": side,
        "status": str(status_value or "").lower(),
        "notional_usdt": notional,
        "entry_price": entry_price,
        "base_size": base_size,
        "timestamp": timestamp,
    }


async def _resolve_position_marks(
    state, positions: Iterable[Mapping[str, Any]]
) -> Dict[Tuple[str, str], float]:
    runtime = getattr(state, "derivatives", None)
    venues = getattr(runtime, "venues", None) if runtime else None
    marks: Dict[Tuple[str, str], float] = {}
    if not venues:
        return marks
    tasks = []
    metadata: List[Tuple[str, str]] = []
    for entry in positions:
        legs = entry.get("legs") if isinstance(entry, Mapping) else None
        if not isinstance(legs, Iterable):
            legs = []
        for leg in legs:
            if not isinstance(leg, Mapping):
                continue
            venue = str(leg.get("venue") or "")
            symbol = str(leg.get("symbol") or "").upper()
            if not venue or not symbol:
                continue
            key = (venue, symbol)
            if key in marks or key in metadata:
                continue
            venue_runtime = venues.get(venue.replace("-", "_"))
            if not venue_runtime:
                continue
            tasks.append(asyncio.to_thread(_fetch_mark_price, venue_runtime.client, symbol))
            metadata.append(key)
    if not tasks:
        return marks
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for key, result in zip(metadata, results):
        if isinstance(result, Exception) or result is None:
            continue
        try:
            marks[key] = float(result)
        except (TypeError, ValueError):
            continue
    return marks


def _fetch_mark_price(client, symbol: str) -> float | None:
    for candidate in _symbol_candidates(symbol):
        try:
            data = client.get_mark_price(candidate)
        except Exception:
            continue
        price: float | None = None
        if isinstance(data, Mapping):
            price = data.get("price") or data.get("markPrice") or data.get("last")
        elif data is not None:
            price = data
        if price is None:
            continue
        try:
            return float(price)
        except (TypeError, ValueError):
            continue
    return None


def _symbol_candidates(symbol: str) -> List[str]:
    base = str(symbol or "").upper()
    candidates = [base]
    if "-" not in base and base.endswith("USDT"):
        prefix = base[:-4]
        candidates.append(f"{prefix}-USDT-SWAP")
    return candidates


__all__ = ["build_positions_snapshot"]

