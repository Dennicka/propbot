from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from positions import list_positions

from ..analytics import calc_attribution
from ..ledger import fetch_events
from ..risk.core import FeatureFlags
from ..services import runtime
from ..strategy.pnl_tracker import get_strategy_pnl_tracker
from ..strategy_pnl import snapshot_all as snapshot_strategy_pnl
from .positions_view import build_positions_snapshot


def _env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _exclude_simulated() -> bool:
    try:
        return FeatureFlags.exclude_dry_run_from_pnl()
    except Exception:
        return _env_flag("EXCLUDE_DRY_RUN_FROM_PNL", True)


def _filtered_items(
    items: Iterable[Mapping[str, Any]] | None, *, exclude_simulated: bool
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for entry in items or []:
        if not isinstance(entry, Mapping):
            continue
        if exclude_simulated and entry.get("simulated"):
            continue
        filtered.append(dict(entry))
    return filtered


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalise_name(value: object, fallback: str) -> str:
    name = str(value or "").strip()
    return name or fallback


def _build_trade_events(positions: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for position in positions:
        if not isinstance(position, Mapping):
            continue
        strategy = _normalise_name(position.get("strategy"), "unknown")
        simulated = bool(position.get("simulated"))
        status = str(position.get("status") or "").lower()
        legs = position.get("legs")
        legs_iterable: list[Mapping[str, Any]] = []
        if isinstance(legs, Iterable):
            for leg in legs:
                if isinstance(leg, Mapping):
                    legs_iterable.append(leg)
        if not legs_iterable:
            continue
        realized_total = _coerce_float(
            position.get("pnl_usdt") or position.get("realized_pnl_usdt")
        )
        realized_share = realized_total / len(legs_iterable) if legs_iterable else 0.0
        for leg in legs_iterable:
            venue = _normalise_name(
                leg.get("venue") or position.get("long_venue") or position.get("short_venue"),
                "unknown",
            )
            if not venue:
                continue
            unrealized = _coerce_float(leg.get("unrealized_pnl_usdt"))
            notional = abs(_coerce_float(leg.get("notional_usdt") or position.get("notional_usdt")))
            liquidity = str(
                leg.get("liquidity") or leg.get("role") or leg.get("side") or "taker"
            ).lower()
            event = {
                "strategy": strategy,
                "venue": venue,
                "realized": (
                    realized_share if status in {"closed", "closing", "closed_partial"} else 0.0
                ),
                "unrealized": unrealized,
                "notional": notional,
                "liquidity": liquidity,
                "simulated": simulated,
                "rolling_30d_notional": leg.get("rolling_30d_notional")
                or position.get("rolling_30d_notional"),
            }
            events.append(event)
    return events


def _load_funding_events(limit: int = 200) -> list[dict[str, Any]]:
    try:
        events = fetch_events(limit=limit, order="desc")
    except Exception:
        return []
    funding_rows: list[dict[str, Any]] = []
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
        strategy = _normalise_name(
            payload.get("strategy") or payload.get("strategy_name"), "unknown"
        )
        venue = _normalise_name(
            payload.get("venue") or payload.get("exchange") or event.get("venue"), "unknown"
        )
        simulated = bool(payload.get("simulated") or payload.get("dry_run"))
        symbol = _normalise_name(
            payload.get("symbol")
            or payload.get("pair")
            or payload.get("instrument")
            or payload.get("asset")
            or event.get("symbol"),
            "unknown",
        )
        funding_rows.append(
            {
                "strategy": strategy,
                "venue": venue,
                "symbol": symbol,
                "amount": amount,
                "ts": event.get("ts"),
                "simulated": simulated,
            }
        )
    return funding_rows


async def build_pnl_attribution() -> dict[str, Any]:
    """Collect trade, fee, rebate and funding data to build a PnL attribution snapshot."""

    state = runtime.get_state()
    positions = list_positions()
    positions_snapshot = await build_positions_snapshot(state, positions)
    trades_raw = _build_trade_events(positions_snapshot.get("positions", []))

    exclude_sim = _exclude_simulated()

    trades_basis = _filtered_items(trades_raw, exclude_simulated=exclude_sim)
    tracker_snapshot = get_strategy_pnl_tracker().snapshot(exclude_simulated=exclude_sim)
    strategy_totals = snapshot_strategy_pnl()

    target_realized: dict[str, float] = {}
    for name, entry in tracker_snapshot.items():
        if not isinstance(entry, Mapping):
            continue
        strategy_name = _normalise_name(name, "unknown")
        tracker_value = _coerce_float(
            entry.get("realized_today")
            or entry.get("realized_pnl_today")
            or entry.get("realized_7d")
        )
        target_realized[strategy_name] = tracker_value
    for name, entry in strategy_totals.items():
        if not isinstance(entry, Mapping):
            continue
        strategy_name = _normalise_name(name, "unknown")
        if strategy_name not in target_realized:
            target_realized[strategy_name] = _coerce_float(entry.get("realized_pnl_today"))
        elif math.isclose(target_realized[strategy_name], 0.0, abs_tol=1e-12):
            fallback_value = _coerce_float(entry.get("realized_pnl_today"))
            if not math.isclose(fallback_value, 0.0, abs_tol=1e-12):
                target_realized[strategy_name] = fallback_value

    if target_realized:
        by_strategy_realized = defaultdict(float)
        for trade in trades_basis:
            by_strategy_realized[trade["strategy"]] += _coerce_float(trade.get("realized"))

        adjustments: list[dict[str, Any]] = []
        for strategy, tracker_realized in target_realized.items():
            basis_value = by_strategy_realized.get(strategy, 0.0)
            delta = tracker_realized - basis_value
            if math.isclose(delta, 0.0, abs_tol=1e-9):
                continue
            adjustments.append(
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
        trades = trades_basis + adjustments if adjustments else trades_basis
    else:
        trades = trades_basis

    fees_raw: list[dict[str, Any]] = []
    rebates_raw: list[dict[str, Any]] = []
    funding_raw = _load_funding_events()

    fees_events = _filtered_items(fees_raw, exclude_simulated=exclude_sim)
    rebates_events = _filtered_items(rebates_raw, exclude_simulated=exclude_sim)
    funding_events = _filtered_items(funding_raw, exclude_simulated=exclude_sim)

    attribution = calc_attribution(
        trades,
        fees_events,
        rebates_events,
        funding_events,
        exclude_sim=exclude_sim,
    )
    meta = dict(attribution.get("meta") or {})
    meta.update(
        {
            "trades_count": len(trades_basis),
            "funding_events_count": len(funding_events),
            "fees_event_count": len(fees_events),
            "rebate_event_count": len(rebates_events),
        }
    )
    if "exclude_simulated" not in meta:
        meta["exclude_simulated"] = exclude_sim

    return {
        "generated_at": _iso_now(),
        "by_strategy": attribution.get("by_strategy", {}),
        "by_venue": attribution.get("by_venue", {}),
        "totals": attribution.get("totals", {}),
        "meta": meta,
        "simulated_excluded": attribution.get("simulated_excluded", exclude_sim),
    }


__all__ = ["build_pnl_attribution"]
