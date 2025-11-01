from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping

from positions import list_positions

from ..analytics import calc_attribution
from ..ledger import fetch_events
from ..services import runtime
from ..strategy_pnl import snapshot_all as snapshot_strategy_pnl
from ..strategy.pnl_tracker import get_strategy_pnl_tracker
from .positions_view import build_positions_snapshot


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalise_name(value: object, fallback: str) -> str:
    name = str(value or "").strip()
    return name or fallback


def _build_trade_events(positions: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for position in positions:
        if not isinstance(position, Mapping):
            continue
        strategy = _normalise_name(position.get("strategy"), "unknown")
        simulated = bool(position.get("simulated"))
        status = str(position.get("status") or "").lower()
        legs = position.get("legs")
        legs_iterable: List[Mapping[str, Any]] = []
        if isinstance(legs, Iterable):
            for leg in legs:
                if isinstance(leg, Mapping):
                    legs_iterable.append(leg)
        if not legs_iterable:
            continue
        realized_total = _coerce_float(position.get("pnl_usdt") or position.get("realized_pnl_usdt"))
        realized_share = realized_total / len(legs_iterable) if legs_iterable else 0.0
        for leg in legs_iterable:
            venue = _normalise_name(leg.get("venue") or position.get("long_venue") or position.get("short_venue"), "unknown")
            if not venue:
                continue
            unrealized = _coerce_float(leg.get("unrealized_pnl_usdt"))
            notional = abs(_coerce_float(leg.get("notional_usdt") or position.get("notional_usdt")))
            liquidity = str(leg.get("liquidity") or leg.get("role") or leg.get("side") or "taker").lower()
            event = {
                "strategy": strategy,
                "venue": venue,
                "realized": realized_share if status in {"closed", "closing", "closed_partial"} else 0.0,
                "unrealized": unrealized,
                "notional": notional,
                "liquidity": liquidity,
                "simulated": simulated,
                "rolling_30d_notional": leg.get("rolling_30d_notional") or position.get("rolling_30d_notional"),
            }
            events.append(event)
    return events


def _load_funding_events(limit: int = 200) -> List[Dict[str, Any]]:
    try:
        events = fetch_events(limit=limit, order="desc")
    except Exception:
        return []
    funding_rows: List[Dict[str, Any]] = []
    for event in events:
        code = str(event.get("code") or event.get("type") or "").lower()
        if code not in {"funding", "funding_payment", "funding_settlement"}:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        amount = _coerce_float(payload.get("amount") or payload.get("pnl") or event.get("amount"))
        if amount == 0.0:
            continue
        strategy = _normalise_name(payload.get("strategy") or payload.get("strategy_name"), "unknown")
        venue = _normalise_name(payload.get("venue") or payload.get("exchange") or event.get("venue"), "unknown")
        simulated = bool(payload.get("simulated") or payload.get("dry_run"))
        funding_rows.append(
            {
                "strategy": strategy,
                "venue": venue,
                "amount": amount,
                "ts": event.get("ts"),
                "simulated": simulated,
            }
        )
    return funding_rows


async def build_pnl_attribution() -> Dict[str, Any]:
    """Collect trade, fee, rebate and funding data to build a PnL attribution snapshot."""

    state = runtime.get_state()
    positions = list_positions()
    positions_snapshot = await build_positions_snapshot(state, positions)
    trades = _build_trade_events(positions_snapshot.get("positions", []))

    tracker_snapshot = get_strategy_pnl_tracker().snapshot()
    strategy_totals = snapshot_strategy_pnl()

    # Provide per-strategy realised adjustments from tracker/state when positions do not include them yet.
    realized_by_strategy: Dict[str, float] = defaultdict(float)
    for name, entry in tracker_snapshot.items():
        if not isinstance(entry, Mapping):
            continue
        realized_by_strategy[_normalise_name(name, "unknown")] += _coerce_float(entry.get("realized_7d"))
    for name, entry in strategy_totals.items():
        if not isinstance(entry, Mapping):
            continue
        realized_by_strategy[_normalise_name(name, "unknown")] += _coerce_float(entry.get("realized_pnl_today"))

    if realized_by_strategy:
        by_strategy_realized = defaultdict(float)
        for trade in trades:
            by_strategy_realized[trade["strategy"]] += _coerce_float(trade.get("realized"))
        for strategy, tracker_realized in realized_by_strategy.items():
            delta = tracker_realized - by_strategy_realized.get(strategy, 0.0)
            if abs(delta) <= 1e-9:
                continue
            trades.append(
                {
                    "strategy": strategy,
                    "venue": "tracker-adjustment",
                    "realized": delta,
                    "unrealized": 0.0,
                    "notional": 0.0,
                    "liquidity": "taker",
                    "simulated": False,
                }
            )

    fees_events: List[Dict[str, Any]] = []
    rebates_events: List[Dict[str, Any]] = []
    funding_events = _load_funding_events()

    attribution = calc_attribution(trades, fees_events, rebates_events, funding_events)
    meta = dict(attribution.get("meta") or {})
    meta.update(
        {
            "trades_count": len(trades),
            "funding_events_count": len(funding_events),
            "fees_event_count": len(fees_events),
            "rebate_event_count": len(rebates_events),
        }
    )

    return {
        "generated_at": _iso_now(),
        "by_strategy": attribution.get("by_strategy", {}),
        "by_venue": attribution.get("by_venue", {}),
        "totals": attribution.get("totals", {}),
        "meta": meta,
    }


__all__ = ["build_pnl_attribution"]
