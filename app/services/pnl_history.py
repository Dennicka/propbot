"""Helpers for capturing rolling exposure and PnL snapshots."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from positions import list_positions

from ..services import portfolio
from pnl_history_store import append_snapshot


_DEFAULT_MAX_HISTORY = 288
_OPEN_STATUSES = {"open", "partial"}


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _aggregate_leg_notional(legs: Iterable[Mapping[str, Any]], target: Dict[str, float]) -> None:
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        venue = str(leg.get("venue") or "")
        if not venue:
            continue
        notional = _coerce_float(leg.get("notional_usdt"))
        if notional <= 0.0:
            # fall back to base size * entry price if available
            entry_price = _coerce_float(leg.get("entry_price"))
            base_size = _coerce_float(leg.get("base_size"))
            notional = abs(base_size * entry_price)
        if notional <= 0.0:
            continue
        target[venue] += abs(notional)


def _fallback_leg_notional(position: Mapping[str, Any], target: Dict[str, float]) -> None:
    notional = abs(_coerce_float(position.get("notional_usdt")))
    if notional <= 0.0:
        return
    long_venue = str(position.get("long_venue") or "")
    short_venue = str(position.get("short_venue") or "")
    if long_venue:
        target[long_venue] += notional
    if short_venue:
        target[short_venue] += notional


def _summarise_positions() -> dict[str, Any]:
    real_exposure: Dict[str, float] = defaultdict(float)
    simulated_exposure: Dict[str, float] = defaultdict(float)
    open_count = 0
    partial_count = 0
    simulated_count = 0

    for position in list_positions():
        status = str(position.get("status") or "").lower()
        simulated = bool(position.get("simulated")) or status == "simulated"
        include_for_real = (status in _OPEN_STATUSES) and not simulated
        include_for_sim = simulated and (status in _OPEN_STATUSES or status == "simulated")

        legs = position.get("legs")
        target = simulated_exposure if simulated else real_exposure

        if include_for_real or include_for_sim:
            if isinstance(legs, Iterable):
                _aggregate_leg_notional(legs, target)
            else:
                _fallback_leg_notional(position, target)
        elif not simulated and isinstance(legs, Iterable) and status in {"closed", "closing"}:
            # no exposure contribution for closed positions
            continue

        if include_for_real:
            if status == "open":
                open_count += 1
            elif status == "partial":
                partial_count += 1
        elif include_for_sim:
            simulated_count += 1

    return {
        "real": {
            "per_venue": dict(sorted(real_exposure.items())),
            "total": sum(real_exposure.values()),
            "open_positions": open_count,
            "partial_positions": partial_count,
        },
        "simulated": {
            "per_venue": dict(sorted(simulated_exposure.items())),
            "total": sum(simulated_exposure.values()),
            "positions": simulated_count,
        },
    }


async def record_snapshot(*, reason: str | None = None, max_entries: int | None = None) -> dict[str, Any]:
    """Capture the current exposure and PnL snapshot and persist it."""

    if max_entries is None:
        max_entries = _DEFAULT_MAX_HISTORY

    portfolio_snapshot = await portfolio.snapshot()
    summary = _summarise_positions()
    pnl_totals = dict(getattr(portfolio_snapshot, "pnl_totals", {}))
    snapshot_payload: dict[str, Any] = {
        "timestamp": _ts(),
        "reason": reason or "auto",  # informational only
        "unrealized_pnl_total": float(pnl_totals.get("unrealized", 0.0)),
        "pnl_totals": pnl_totals,
        "total_exposure_usd": summary["real"]["per_venue"],
        "total_exposure_usd_total": summary["real"]["total"],
        "open_positions": summary["real"]["open_positions"],
        "partial_positions": summary["real"]["partial_positions"],
        "open_positions_total": summary["real"]["open_positions"] + summary["real"]["partial_positions"],
        "simulated": summary["simulated"],
    }
    append_snapshot(snapshot_payload, max_entries=max_entries)
    return snapshot_payload


async def ensure_snapshot_background(*, reason: str | None = None) -> None:
    """Fire-and-forget helper that records a snapshot in the background."""

    async def _task() -> None:
        try:
            await record_snapshot(reason=reason)
        except Exception:
            pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(record_snapshot(reason=reason))
        return
    loop.create_task(_task())


__all__ = ["ensure_snapshot_background", "record_snapshot"]
