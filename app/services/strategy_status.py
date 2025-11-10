from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict

from ..strategy_budget import get_strategy_budget_manager
from ..strategy_pnl import snapshot_all as snapshot_strategy_pnl
from ..strategy_risk import get_strategy_risk_manager


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def build_strategy_status() -> Dict[str, Dict[str, Any]]:
    """Return a merged status snapshot for each strategy."""

    risk_snapshot = get_strategy_risk_manager().full_snapshot()
    strategies_payload = (
        risk_snapshot.get("strategies") if isinstance(risk_snapshot, Mapping) else {}
    )
    risk_strategies = strategies_payload if isinstance(strategies_payload, Mapping) else {}

    budget_snapshot = get_strategy_budget_manager().snapshot()
    pnl_snapshot = snapshot_strategy_pnl()

    strategy_names = set(risk_strategies) | set(budget_snapshot) | set(pnl_snapshot)
    statuses: Dict[str, Dict[str, Any]] = {}

    for name in sorted(strategy_names):
        risk_entry = risk_strategies.get(name)
        risk_mapping = risk_entry if isinstance(risk_entry, Mapping) else {}
        state_payload = (
            risk_mapping.get("state") if isinstance(risk_mapping.get("state"), Mapping) else {}
        )
        pnl_entry = pnl_snapshot.get(name) if isinstance(pnl_snapshot.get(name), Mapping) else {}
        budget_entry = (
            budget_snapshot.get(name) if isinstance(budget_snapshot.get(name), Mapping) else {}
        )

        frozen = bool(
            (state_payload.get("frozen") if isinstance(state_payload, Mapping) else False)
            or risk_mapping.get("frozen")
        )
        freeze_reason = ""
        if isinstance(state_payload, Mapping) and state_payload:
            freeze_reason = str(
                state_payload.get("freeze_reason")
                or state_payload.get("reason")
                or risk_mapping.get("freeze_reason")
                or risk_mapping.get("reason")
                or ""
            )
        else:
            freeze_reason = str(
                risk_mapping.get("freeze_reason") or risk_mapping.get("reason") or ""
            )
        breach_reasons = []
        if isinstance(risk_mapping.get("breach_reasons"), list):
            breach_reasons = [str(reason) for reason in risk_mapping.get("breach_reasons")]
        consecutive_failures = _coerce_int(
            (
                state_payload.get("consecutive_failures")
                if isinstance(state_payload, Mapping)
                else None
            ),
            default=0,
        )
        status = {
            "strategy": name,
            "enabled": bool(risk_mapping.get("enabled", True)),
            "frozen": frozen,
            "freeze_reason": freeze_reason,
            "last_breach": breach_reasons[0] if breach_reasons else "",
            "breach_reasons": breach_reasons,
            "consecutive_failures": consecutive_failures,
            "realized_pnl_today": _coerce_float(pnl_entry.get("realized_pnl_today")),
            "realized_pnl_total": _coerce_float(pnl_entry.get("realized_pnl_total")),
            "realized_pnl_7d": _coerce_float(pnl_entry.get("realized_pnl_7d")),
            "max_drawdown_observed": _coerce_float(pnl_entry.get("max_drawdown_observed")),
            "budget_blocked": bool(budget_entry.get("blocked")),
            "budget": {
                "max_notional_usdt": budget_entry.get("max_notional_usdt"),
                "current_notional_usdt": budget_entry.get("current_notional_usdt", 0.0),
                "max_open_positions": budget_entry.get("max_open_positions"),
                "current_open_positions": budget_entry.get("current_open_positions", 0),
            },
            "risk_limits": (
                dict(risk_mapping.get("limits", {}))
                if isinstance(risk_mapping.get("limits"), Mapping)
                else {}
            ),
            "state": dict(state_payload) if isinstance(state_payload, Mapping) else {},
        }
        statuses[name] = status

    return statuses


__all__ = ["build_strategy_status"]
