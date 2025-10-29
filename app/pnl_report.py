from __future__ import annotations

from typing import Any, Mapping

from .capital_manager import get_capital_manager


def _coerce_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_pnl_snapshot(positions_snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a read-only PnL and risk summary for the operator dashboard."""

    snapshot = positions_snapshot or {}
    totals = snapshot.get("totals") if isinstance(snapshot, Mapping) else None
    exposure = snapshot.get("exposure") if isinstance(snapshot, Mapping) else None

    unrealized_pnl = 0.0
    if isinstance(totals, Mapping):
        unrealized_pnl = _coerce_float(totals.get("unrealized_pnl_usdt"))

    total_exposure = 0.0
    if isinstance(exposure, Mapping):
        for payload in exposure.values():
            if not isinstance(payload, Mapping):
                continue
            long_value = _coerce_float(payload.get("long_notional"))
            short_value = _coerce_float(payload.get("short_notional"))
            total_exposure += max(long_value, 0.0) + max(short_value, 0.0)

    manager = get_capital_manager()
    capital_snapshot = manager.snapshot()
    headroom = capital_snapshot.get("headroom")
    if not isinstance(headroom, Mapping):
        headroom = {}

    return {
        "unrealized_pnl_usdt": unrealized_pnl,
        # TODO: persist daily realised PnL snapshots and surface them here.
        "realised_pnl_today_usdt": 0.0,
        "total_exposure_usdt": total_exposure,
        "capital_headroom_per_strategy": dict(headroom),
        "capital_snapshot": capital_snapshot,
        # TODO: write historical daily summaries to a dedicated store for trend analysis.
    }
