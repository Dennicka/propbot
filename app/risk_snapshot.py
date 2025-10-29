from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict

from positions import list_positions

from .services.positions_view import build_positions_snapshot
from .services.runtime import get_state


async def build_risk_snapshot() -> Dict[str, Any]:
    """Aggregate a lightweight risk summary from existing position data."""

    state = get_state()
    positions = list_positions()
    if positions:
        positions_snapshot = await build_positions_snapshot(state, positions)
    else:
        positions_snapshot = {"positions": [], "exposure": {}, "totals": {"unrealized_pnl_usdt": 0.0}}

    per_venue_summary: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"net_exposure_usd": 0.0, "unrealised_pnl_usd": 0.0, "open_positions_count": 0}
    )
    venue_positions: dict[str, set[str]] = defaultdict(set)

    total_notional = 0.0
    partial_legs = 0

    for entry in positions_snapshot.get("positions", []):
        position_id = str(entry.get("id") or "")
        position_status = str(entry.get("status") or "").lower()
        simulated_position = bool(entry.get("simulated"))
        legs = entry.get("legs") or []
        for leg in legs:
            leg_status = str(leg.get("status") or position_status).lower()
            if leg_status not in {"open", "partial"}:
                continue
            if simulated_position or bool(leg.get("simulated")):
                continue
            venue = str(leg.get("venue") or "")
            if not venue:
                continue
            try:
                notional = float(leg.get("notional_usdt") or 0.0)
            except (TypeError, ValueError):
                notional = 0.0
            if notional:
                total_notional += notional
            if leg_status == "partial":
                partial_legs += 1
            try:
                pnl = float(leg.get("unrealized_pnl_usdt") or 0.0)
            except (TypeError, ValueError):
                pnl = 0.0
            summary = per_venue_summary[venue]
            side = str(leg.get("side") or "").lower()
            if side == "short":
                summary["net_exposure_usd"] -= notional
            else:
                summary["net_exposure_usd"] += notional
            summary["unrealised_pnl_usd"] += pnl
            if position_id:
                venue_positions[venue].add(position_id)

    for venue, ids in venue_positions.items():
        summary = per_venue_summary[venue]
        summary["open_positions_count"] = len(ids)

    per_venue = {venue: dict(values) for venue, values in per_venue_summary.items()}

    control = state.control
    safety = state.safety
    autopilot = state.autopilot

    return {
        "total_notional_usd": total_notional,
        "per_venue": per_venue,
        "partial_hedges_count": partial_legs,
        "autopilot_enabled": bool(getattr(autopilot, "enabled", False)),
        "hold_active": bool(getattr(safety, "hold_active", False)),
        "safe_mode": bool(getattr(control, "safe_mode", False)),
        "dry_run_mode": bool(getattr(control, "dry_run_mode", False)),
        "risk_score": "TBD",
    }
