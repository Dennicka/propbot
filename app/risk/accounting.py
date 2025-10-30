"""Lightweight in-memory risk accounting helpers.

The accounting module keeps a simple per-strategy tally of open notionals,
open position counts and realised PnL for the current (local) day.  The state is
purely in-memory and intended to provide fast feedback loops for strategy
execution and UI surfaces.  It intentionally does not persist any state.

Two primitive hooks are exposed to the execution layer:

``record_intent``
    Invoked once a trading strategy decides to place orders.  The helper
    validates that the projected totals remain within the global
    :class:`~app.risk.core.RiskGovernor` caps as well as within a configured
    per-strategy loss budget.  When the allocation is allowed the counters are
    bumped; otherwise the call returns ``ok=False`` together with the latest
    snapshot so the caller can abort gracefully.

``record_fill``
    Called after the trade flow finishes.  It releases the open notionals and
    position counters that were previously reserved and records the realised
    PnL delta.  Losses count against the per-strategy budget.

Snapshots returned by the helpers – and via :func:`get_risk_snapshot` – expose
aggregate totals as well as per-strategy breakdowns that feed the operator UI
and API endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import threading
from typing import Dict, Iterable, Tuple

from ..budget.strategy_budget import StrategyBudgetManager
from ..services.runtime import get_state
from .core import FeatureFlags, RiskGovernor, get_risk_governor

__all__ = [
    "get_risk_snapshot",
    "record_intent",
    "record_fill",
    "set_strategy_budget_cap",
    "reset_risk_accounting_for_tests",
]


@dataclass
class _StrategyAccounting:
    open_notional: float = 0.0
    open_positions: int = 0
    realised_pnl_today: float = 0.0
    simulated_open_notional: float = 0.0
    simulated_open_positions: int = 0
    simulated_realised_pnl_today: float = 0.0
    budget_used: float = 0.0


@dataclass
class _Totals:
    open_notional: float = 0.0
    open_positions: int = 0
    realised_pnl_today: float = 0.0
    simulated_open_notional: float = 0.0
    simulated_open_positions: int = 0
    simulated_realised_pnl_today: float = 0.0
    budget_used: float = 0.0


_LOCK = threading.RLock()
_STRATEGY_STATE: Dict[str, _StrategyAccounting] = {}
_TOTALS = _Totals()
_CURRENT_DATE: date | None = None
_BUDGET_MANAGER = StrategyBudgetManager()
_LAST_DENIAL: dict[str, object] | None = None


def _today() -> date:
    return date.today()


def _reset_for_new_day_unlocked() -> None:
    """Reset daily counters when the local date rolls over."""

    global _CURRENT_DATE
    _CURRENT_DATE = _today()
    _TOTALS.realised_pnl_today = 0.0
    _TOTALS.simulated_realised_pnl_today = 0.0
    _TOTALS.budget_used = 0.0
    for entry in _STRATEGY_STATE.values():
        entry.realised_pnl_today = 0.0
        entry.simulated_realised_pnl_today = 0.0
        entry.budget_used = 0.0


def _maybe_reset_day_unlocked() -> None:
    global _CURRENT_DATE
    if _CURRENT_DATE is None or _CURRENT_DATE != _today():
        _reset_for_new_day_unlocked()


def _strategy_entry(strategy: str) -> _StrategyAccounting:
    strategy_key = strategy.strip()
    if not strategy_key:
        raise ValueError("strategy name must be provided")
    entry = _STRATEGY_STATE.get(strategy_key)
    if entry is None:
        entry = _StrategyAccounting()
        _STRATEGY_STATE[strategy_key] = entry
    return entry


def _tolerance() -> float:
    return 1e-6


def _budget_limit(strategy: str) -> float | None:
    try:
        return _BUDGET_MANAGER.get_cap(strategy)
    except Exception:  # pragma: no cover - defensive
        return None


def _budget_breached(entry: _StrategyAccounting, *, strategy: str) -> bool:
    limit = _budget_limit(strategy)
    if limit is None:
        return False
    return entry.budget_used >= (limit - _tolerance())


def _totals_breaches(governor: RiskGovernor) -> Iterable[str]:
    breaches: list[str] = []
    caps = governor.caps
    if _TOTALS.open_notional > caps.max_total_notional_usdt + _tolerance():
        breaches.append("max_total_notional_usdt")
    if _TOTALS.open_positions > caps.max_open_positions:
        breaches.append("max_open_positions")
    return breaches


def _strategy_snapshot(strategy: str, entry: _StrategyAccounting) -> dict:
    limit = _budget_limit(strategy)
    breaches: list[str] = []
    if _budget_breached(entry, strategy=strategy):
        breaches.append("budget_exhausted")
    snapshot = {
        "open_notional": entry.open_notional,
        "open_positions": entry.open_positions,
        "realized_pnl_today": entry.realised_pnl_today,
        "budget": {"used": entry.budget_used, "limit": limit},
        "breaches": breaches,
        "simulated": {
            "open_notional": entry.simulated_open_notional,
            "open_positions": entry.simulated_open_positions,
            "realized_pnl_today": entry.simulated_realised_pnl_today,
        },
    }
    return snapshot


def _snapshot_unlocked() -> dict:
    governor = get_risk_governor()
    per_strategy = {
        name: _strategy_snapshot(name, entry)
        for name, entry in sorted(_STRATEGY_STATE.items())
    }
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "open_notional": _TOTALS.open_notional,
            "open_positions": _TOTALS.open_positions,
            "realized_pnl_today": _TOTALS.realised_pnl_today,
            "budget_used": _TOTALS.budget_used,
            "breaches": list(_totals_breaches(governor)),
            "simulated": {
                "open_notional": _TOTALS.simulated_open_notional,
                "open_positions": _TOTALS.simulated_open_positions,
                "realized_pnl_today": _TOTALS.simulated_realised_pnl_today,
            },
        },
        "per_strategy": per_strategy,
    }
    if _LAST_DENIAL is not None:
        snapshot["last_denial"] = dict(_LAST_DENIAL)
    return snapshot


def get_risk_snapshot() -> dict:
    """Return a copy of the current accounting snapshot."""

    with _LOCK:
        _maybe_reset_day_unlocked()
        return _snapshot_unlocked()


def record_intent(strategy: str, notional: float, *, simulated: bool) -> Tuple[dict, bool]:
    """Record an execution intent and validate risk constraints.

    Parameters
    ----------
    strategy:
        Strategy identifier.
    notional:
        Requested notional (USDT) for the trade.  Negative values are treated as
        zero.
    simulated:
        Flag indicating whether the run is simulated (dry-run/safe-mode).
    """

    notional_value = max(float(notional or 0.0), 0.0)
    with _LOCK:
        _maybe_reset_day_unlocked()
        entry = _strategy_entry(strategy)
        state = get_state()
        control = getattr(state, "control", None)
        dry_run = bool(getattr(control, "dry_run", False)) or FeatureFlags.dry_run_mode() or simulated
        governor = get_risk_governor()
        projected_notional = _TOTALS.open_notional + (0.0 if simulated else notional_value)
        projected_positions = _TOTALS.open_positions + (0 if simulated else 1)
        limit = _budget_limit(strategy)
        validation = governor.validate(
            intent_notional=projected_notional,
            projected_positions=projected_positions,
            dry_run=dry_run,
            current_total_notional=_TOTALS.open_notional,
            current_open_positions=_TOTALS.open_positions,
            budget_limit=limit,
            budget_used=entry.budget_used,
        )
        global _LAST_DENIAL
        if not validation.get("ok", False):
            _LAST_DENIAL = {"source": "accounting", "strategy": strategy, **validation}
            snapshot = _snapshot_unlocked()
            return snapshot, False

        _LAST_DENIAL = None

        if simulated:
            entry.simulated_open_notional += notional_value
            entry.simulated_open_positions += 1
            _TOTALS.simulated_open_notional += notional_value
            _TOTALS.simulated_open_positions += 1
            return _snapshot_unlocked(), True

        entry.open_notional += notional_value
        entry.open_positions += 1
        _TOTALS.open_notional += notional_value
        _TOTALS.open_positions += 1
        return _snapshot_unlocked(), True


def record_fill(strategy: str, notional: float, pnl_delta: float, *, simulated: bool) -> dict:
    """Release the reserved exposure and account for realised PnL."""

    notional_value = max(float(notional or 0.0), 0.0)
    pnl_value = float(pnl_delta or 0.0)

    with _LOCK:
        _maybe_reset_day_unlocked()
        entry = _strategy_entry(strategy)
        if simulated:
            entry.simulated_open_notional = max(entry.simulated_open_notional - notional_value, 0.0)
            entry.simulated_open_positions = max(entry.simulated_open_positions - 1, 0)
            entry.simulated_realised_pnl_today += pnl_value
            _TOTALS.simulated_open_notional = max(_TOTALS.simulated_open_notional - notional_value, 0.0)
            _TOTALS.simulated_open_positions = max(_TOTALS.simulated_open_positions - 1, 0)
            _TOTALS.simulated_realised_pnl_today += pnl_value
            return _snapshot_unlocked()

        entry.open_notional = max(entry.open_notional - notional_value, 0.0)
        entry.open_positions = max(entry.open_positions - 1, 0)
        entry.realised_pnl_today += pnl_value
        _TOTALS.open_notional = max(_TOTALS.open_notional - notional_value, 0.0)
        _TOTALS.open_positions = max(_TOTALS.open_positions - 1, 0)
        _TOTALS.realised_pnl_today += pnl_value

        if pnl_value < 0:
            loss = abs(pnl_value)
            entry.budget_used += loss
            _TOTALS.budget_used += loss

        return _snapshot_unlocked()


def set_strategy_budget_cap(strategy: str, cap: float) -> None:
    """Configure the per-strategy loss budget cap."""

    with _LOCK:
        _BUDGET_MANAGER.set_cap(strategy, cap)


def reset_risk_accounting_for_tests() -> None:
    """Helper to reset state between tests."""

    with _LOCK:
        _STRATEGY_STATE.clear()
        global _CURRENT_DATE
        _CURRENT_DATE = None
        _TOTALS.open_notional = 0.0
        _TOTALS.open_positions = 0
        _TOTALS.realised_pnl_today = 0.0
        _TOTALS.simulated_open_notional = 0.0
        _TOTALS.simulated_open_positions = 0
        _TOTALS.simulated_realised_pnl_today = 0.0
        _TOTALS.budget_used = 0.0
        global _LAST_DENIAL
        _LAST_DENIAL = None
