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
from typing import Dict, Iterable, Mapping, Tuple

from ..budget.strategy_budget import StrategyBudgetManager
from ..services.runtime import get_state
from .core import FeatureFlags, RiskGovernor, get_risk_governor
from .daily_loss import (
    get_daily_loss_cap,
    get_daily_loss_cap_state,
    is_daily_loss_cap_breached,
    reset_daily_loss_cap_for_tests,
)
from .telemetry import record_risk_skip

__all__ = [
    "get_risk_snapshot",
    "get_bot_loss_cap_state",
    "record_intent",
    "record_fill",
    "set_strategy_budget_cap",
    "reset_strategy_budget_usage",
    "reset_risk_accounting_for_tests",
    "is_loss_cap_breached",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _current_epoch_day() -> int:
    ts = _utc_now()
    return int(ts.timestamp() // 86_400)


_DAILY_LOSS_CAP = get_daily_loss_cap()


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


def get_bot_loss_cap_state() -> dict[str, float | bool | None]:
    return get_daily_loss_cap_state()


def is_loss_cap_breached() -> bool:
    return is_daily_loss_cap_breached()


def _reason_code_from_details(
    details: Mapping[str, object] | None, default: str = "other_risk"
) -> str:
    if isinstance(details, Mapping):
        type_value = str(details.get("type") or "").lower()
        if type_value == "caps":
            return "caps_exceeded"
        if type_value == "budgets":
            return "budget_exceeded"
        breach = str(details.get("breach") or "").lower()
        if breach.startswith("max_"):
            return "caps_exceeded"
        if breach.startswith("budget"):
            return "budget_exceeded"
    return default


def _today() -> date:
    return datetime.now(timezone.utc).date()


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
    _DAILY_LOSS_CAP.maybe_reset()


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


def _budget_info(strategy: str, entry: _StrategyAccounting) -> Mapping[str, object]:
    try:
        info: Mapping[str, object] = _BUDGET_MANAGER.get_budget_state(strategy)
    except Exception:  # pragma: no cover - defensive
        info = {}
    used = float(info.get("used_today_usdt") or 0.0)
    entry.budget_used = used
    return info


def _recalculate_budget_totals() -> None:
    _TOTALS.budget_used = sum(item.budget_used for item in _STRATEGY_STATE.values())


def _budget_blocked(budget_info: Mapping[str, object]) -> bool:
    limit = budget_info.get("limit_usdt")
    if limit is None:
        return False
    try:
        limit_value = float(limit)
    except (TypeError, ValueError):
        return False
    used = float(budget_info.get("used_today_usdt") or 0.0)
    return used >= (limit_value - _tolerance())


def _epoch_day_to_iso(epoch_day: object) -> str | None:
    try:
        day = int(epoch_day)
    except (TypeError, ValueError):
        return None
    if day < 0:
        return None
    ts = datetime.fromtimestamp(day * 86_400, tz=timezone.utc)
    return ts.isoformat()


def _totals_breaches(governor: RiskGovernor) -> Iterable[str]:
    breaches: list[str] = []
    caps = governor.caps
    if _TOTALS.open_notional > caps.max_total_notional_usdt + _tolerance():
        breaches.append("max_total_notional_usdt")
    if _TOTALS.open_positions > caps.max_open_positions:
        breaches.append("max_open_positions")
    return breaches


def _strategy_snapshot(
    strategy: str, entry: _StrategyAccounting, budget_info: Mapping[str, object]
) -> dict:
    breaches: list[str] = []
    blocked = _budget_blocked(budget_info)
    if blocked:
        breaches.append("budget_exhausted")
    limit = budget_info.get("limit_usdt")
    try:
        limit_value = float(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit_value = None
    used = float(budget_info.get("used_today_usdt") or entry.budget_used)
    remaining: float | None
    if limit_value is None:
        remaining = None
    else:
        remaining = limit_value - used
    budget_payload = {
        "used": used,
        "limit": limit_value,
        "used_today_usdt": used,
        "limit_usdt": limit_value,
        "remaining_usdt": remaining,
        "last_reset_epoch_day": budget_info.get("last_reset_epoch_day"),
        "last_reset_ts_utc": _epoch_day_to_iso(budget_info.get("last_reset_epoch_day")),
    }
    snapshot = {
        "open_notional": entry.open_notional,
        "open_positions": entry.open_positions,
        "realized_pnl_today": entry.realised_pnl_today,
        "budget": budget_payload,
        "blocked_by_budget": blocked,
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
    try:
        budget_snapshot = _BUDGET_MANAGER.snapshot()
    except Exception:  # pragma: no cover - defensive
        budget_snapshot = {}
    per_strategy: dict[str, dict[str, object]] = {}
    for name, entry in sorted(_STRATEGY_STATE.items()):
        info = budget_snapshot.get(name)
        if not isinstance(info, Mapping):
            info = _budget_info(name, entry)
        else:
            entry.budget_used = float(info.get("used_today_usdt") or entry.budget_used)
        per_strategy[name] = _strategy_snapshot(name, entry, info)
    _recalculate_budget_totals()
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
    daily_loss_snapshot = get_bot_loss_cap_state()
    snapshot["bot_loss_cap"] = daily_loss_snapshot
    snapshot["daily_loss_cap"] = daily_loss_snapshot
    if _LAST_DENIAL is not None:
        snapshot["last_denial"] = dict(_LAST_DENIAL)
    return snapshot


def get_risk_snapshot() -> dict:
    """Return a copy of the current accounting snapshot."""

    with _LOCK:
        _maybe_reset_day_unlocked()
        _DAILY_LOSS_CAP.maybe_reset()
        return _snapshot_unlocked()


def record_intent(
    strategy: str, notional: float, *, simulated: bool
) -> Tuple[dict, Dict[str, object]]:
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

    Returns
    -------
    tuple(dict, dict)
        Snapshot of the current accounting state together with a result payload
        describing whether the allocation was accepted.  When blocked the
        result contains ``ok=False`` and a ``reason`` code.
    """

    notional_value = max(float(notional or 0.0), 0.0)
    result: Dict[str, object] = {
        "ok": True,
        "state": "RECORDED",
        "reason": None,
        "strategy": strategy,
    }
    with _LOCK:
        global _LAST_DENIAL
        _maybe_reset_day_unlocked()
        _DAILY_LOSS_CAP.maybe_reset()
        entry = _strategy_entry(strategy)
        budget_info = _budget_info(strategy, entry)
        _recalculate_budget_totals()
        state = get_state()
        control = getattr(state, "control", None)
        dry_run = (
            bool(getattr(control, "dry_run", False))
            or FeatureFlags.dry_run_mode()
            or simulated
        )
        runtime_dry_run_mode = bool(getattr(control, "dry_run_mode", False))
        budgets_enabled = (
            FeatureFlags.risk_checks_enabled()
            and FeatureFlags.enforce_budgets()
            and not runtime_dry_run_mode
        )
        enforce_budget_now = budgets_enabled and not dry_run
        governor = get_risk_governor()
        if (
            FeatureFlags.risk_checks_enabled()
            and FeatureFlags.enforce_daily_loss_cap()
            and not dry_run
            and is_daily_loss_cap_breached()
        ):
            record_risk_skip(strategy, "daily_loss_cap")
            cap_snapshot = get_bot_loss_cap_state()
            loss_cap_details = {
                "daily_loss_cap": cap_snapshot,
                "bot_loss_cap": cap_snapshot,
            }
            failure_payload = {
                "source": "accounting",
                "strategy": strategy,
                "ok": False,
                "state": "SKIPPED_BY_RISK",
                "reason": "DAILY_LOSS_CAP",
                "details": loss_cap_details,
            }
            _LAST_DENIAL = dict(failure_payload)
            snapshot = _snapshot_unlocked()
            failed_result = dict(result)
            failed_result.update(
                {
                    "ok": False,
                    "state": "SKIPPED_BY_RISK",
                    "reason": "DAILY_LOSS_CAP",
                    "details": loss_cap_details,
                }
            )
            return snapshot, failed_result
        projected_notional = _TOTALS.open_notional + (0.0 if simulated else notional_value)
        projected_positions = _TOTALS.open_positions + (0 if simulated else 1)
        validation = governor.validate(
            intent_notional=projected_notional,
            projected_positions=projected_positions,
            dry_run=dry_run,
            current_total_notional=_TOTALS.open_notional,
            current_open_positions=_TOTALS.open_positions,
            budget_limit=None,
            budget_used=entry.budget_used,
        )
        if not validation.get("ok", False):
            details = validation.get("details")
            reason_code = _reason_code_from_details(details, default="caps_exceeded")
            record_risk_skip(strategy, reason_code)
            denial_payload = {
                "source": "accounting",
                "strategy": strategy,
                "ok": False,
                "state": "SKIPPED_BY_RISK",
                "reason": reason_code,
            }
            if details is not None:
                denial_payload["details"] = details
            _LAST_DENIAL = denial_payload
            snapshot = _snapshot_unlocked()
            failed_result = dict(result)
            failed_result.update(
                {
                    "ok": False,
                    "state": "SKIPPED_BY_RISK",
                    "reason": reason_code,
                }
            )
            if details is not None:
                failed_result["details"] = details
            return snapshot, failed_result

        blocked = _budget_blocked(budget_info)
        if enforce_budget_now and blocked:
            limit = budget_info.get("limit_usdt")
            try:
                limit_value = float(limit) if limit is not None else None
            except (TypeError, ValueError):
                limit_value = None
            used = float(budget_info.get("used_today_usdt") or entry.budget_used)
            details: dict[str, object] = {"used": used}
            if limit_value is not None:
                details["limit"] = limit_value
            record_risk_skip(strategy, "budget_exceeded")
            _LAST_DENIAL = {
                "source": "accounting",
                "strategy": strategy,
                "ok": False,
                "state": "SKIPPED_BY_RISK",
                "reason": "budget_exceeded",
                "details": details,
            }
            snapshot = _snapshot_unlocked()
            failed_result = dict(result)
            failed_result.update(
                {
                    "ok": False,
                    "state": "SKIPPED_BY_RISK",
                    "reason": "budget_exceeded",
                    "details": details,
                }
            )
            return snapshot, failed_result

        _LAST_DENIAL = None

        if simulated:
            entry.simulated_open_notional += notional_value
            entry.simulated_open_positions += 1
            _TOTALS.simulated_open_notional += notional_value
            _TOTALS.simulated_open_positions += 1
            return _snapshot_unlocked(), dict(result)

        entry.open_notional += notional_value
        entry.open_positions += 1
        _TOTALS.open_notional += notional_value
        _TOTALS.open_positions += 1
        return _snapshot_unlocked(), dict(result)


def record_fill(strategy: str, notional: float, pnl_delta: float, *, simulated: bool) -> dict:
    """Release the reserved exposure and account for realised PnL."""

    notional_value = max(float(notional or 0.0), 0.0)
    pnl_value = float(pnl_delta or 0.0)

    with _LOCK:
        _maybe_reset_day_unlocked()
        _DAILY_LOSS_CAP.maybe_reset()
        entry = _strategy_entry(strategy)
        _budget_info(strategy, entry)
        _recalculate_budget_totals()
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
        _DAILY_LOSS_CAP.record_realized(pnl_value)

        if pnl_value < 0:
            loss = abs(pnl_value)
            try:
                updated = _BUDGET_MANAGER.add_usage(strategy, loss)
            except Exception:  # pragma: no cover - defensive
                updated = {}
            if isinstance(updated, Mapping):
                entry.budget_used = float(updated.get("used_today_usdt") or entry.budget_used)
            else:
                entry.budget_used += loss
            _recalculate_budget_totals()

        return _snapshot_unlocked()


def set_strategy_budget_cap(strategy: str, cap: float) -> None:
    """Configure the per-strategy loss budget cap."""

    with _LOCK:
        _BUDGET_MANAGER.set_cap(strategy, cap)
        entry = _STRATEGY_STATE.get(strategy.strip())
        if entry is not None:
            _budget_info(strategy, entry)
            _recalculate_budget_totals()


def reset_strategy_budget_usage(strategy: str) -> dict[str, object]:
    """Reset the recorded budget usage for ``strategy`` and return the state."""

    with _LOCK:
        state: Mapping[str, object]
        try:
            state = _BUDGET_MANAGER.reset_usage(strategy)
        except Exception as exc:  # pragma: no cover - defensive
            raise ValueError(f"unable to reset budget for {strategy}") from exc
        entry = _STRATEGY_STATE.get(strategy.strip())
        if entry is not None:
            entry.budget_used = float(state.get("used_today_usdt") or 0.0)
        _recalculate_budget_totals()
        return dict(state)


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
        _BUDGET_MANAGER.reset_all()
        reset_daily_loss_cap_for_tests()
