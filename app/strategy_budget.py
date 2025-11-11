from __future__ import annotations

import os
import threading
from typing import Any, Mapping

from .runtime_state_store import load_runtime_payload, write_runtime_payload


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _coerce_float(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: object, *, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _coerce_int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _default_max_notional() -> float | None:
    max_total = _env_float("MAX_TOTAL_NOTIONAL_USDT", 150_000.0)
    if max_total > 0:
        return max_total
    per_position = _env_float("MAX_NOTIONAL_PER_POSITION_USDT", 50_000.0)
    max_positions = _env_int("MAX_OPEN_POSITIONS", 3)
    if per_position > 0 and max_positions > 0:
        return per_position * max_positions
    return None


def _default_max_positions() -> int | None:
    max_positions = _env_int("MAX_OPEN_POSITIONS", 3)
    if max_positions > 0:
        return max_positions
    return None


class StrategyBudgetManager:
    """Track per-strategy capital usage and enforce allocation limits."""

    def __init__(
        self,
        *,
        initial_budgets: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        if initial_budgets is not None:
            budgets = self._normalise_budgets(initial_budgets)
            if not budgets:
                budgets = self._default_budgets()
            self._budgets: dict[str, dict[str, float | int | None]] = budgets
            self._persist_unlocked()
        else:
            self._budgets = self._load_or_initialise()

    def _load_or_initialise(self) -> dict[str, dict[str, float | int | None]]:
        payload = load_runtime_payload()
        raw = payload.get("strategy_budgets") if isinstance(payload, Mapping) else None
        budgets = self._normalise_budgets(raw)
        if not budgets:
            budgets = self._default_budgets()
            self._budgets = budgets
            self._persist_unlocked()
        return budgets

    def _normalise_budgets(
        self, payload: Mapping[str, Mapping[str, object]] | None
    ) -> dict[str, dict[str, float | int | None]]:
        if not isinstance(payload, Mapping):
            return {}
        budgets: dict[str, dict[str, float | int | None]] = {}
        for name, raw_entry in payload.items():
            if not isinstance(raw_entry, Mapping):
                continue
            strategy = str(name).strip()
            if not strategy:
                continue
            max_notional = _coerce_float_or_none(raw_entry.get("max_notional_usdt"))
            max_positions = _coerce_int_or_none(raw_entry.get("max_open_positions"))
            current_notional = max(_coerce_float(raw_entry.get("current_notional_usdt")), 0.0)
            current_positions = max(_coerce_int(raw_entry.get("current_open_positions")), 0)
            budgets[strategy] = {
                "max_notional_usdt": max_notional,
                "max_open_positions": max_positions,
                "current_notional_usdt": current_notional,
                "current_open_positions": current_positions,
            }
        return budgets

    def _default_entry(self) -> dict[str, float | int | None]:
        return {
            "max_notional_usdt": _default_max_notional(),
            "max_open_positions": _default_max_positions(),
            "current_notional_usdt": 0.0,
            "current_open_positions": 0,
        }

    def _default_budgets(self) -> dict[str, dict[str, float | int | None]]:
        return {
            "cross_exchange_arb": self._default_entry(),
        }

    def _persist_unlocked(self) -> None:
        payload = load_runtime_payload()
        payload_dict = dict(payload) if isinstance(payload, Mapping) else {}
        payload_dict["strategy_budgets"] = {
            name: {
                "max_notional_usdt": entry.get("max_notional_usdt"),
                "max_open_positions": entry.get("max_open_positions"),
                "current_notional_usdt": entry.get("current_notional_usdt", 0.0),
                "current_open_positions": entry.get("current_open_positions", 0),
            }
            for name, entry in self._budgets.items()
        }
        write_runtime_payload(payload_dict)

    def _ensure_strategy(self, strategy_name: str) -> dict[str, float | int | None]:
        strategy = strategy_name.strip()
        if not strategy:
            raise ValueError("strategy name must be non-empty")
        if strategy not in self._budgets:
            self._budgets[strategy] = self._default_entry()
        return self._budgets[strategy]

    def get_limits(self, strategy_name: str) -> dict[str, float | int | None]:
        with self._lock:
            entry = self._ensure_strategy(strategy_name)
            return dict(entry)

    def can_allocate(
        self,
        strategy_name: str,
        requested_notional: float,
        *,
        requested_positions: int = 1,
    ) -> bool:
        with self._lock:
            entry = self._ensure_strategy(strategy_name)
            requested_notional_value = max(_coerce_float(requested_notional), 0.0)
            requested_positions_value = max(_coerce_int(requested_positions), 0)
            limit_notional = entry.get("max_notional_usdt")
            if (
                limit_notional is not None
                and requested_notional_value + entry.get("current_notional_usdt", 0.0)
                > limit_notional + 1e-9
            ):
                return False
            limit_positions = entry.get("max_open_positions")
            if (
                limit_positions is not None
                and requested_positions_value + entry.get("current_open_positions", 0)
                > limit_positions
            ):
                return False
            return True

    def reserve(
        self,
        strategy_name: str,
        notional: float,
        *,
        positions: int = 1,
    ) -> None:
        with self._lock:
            entry = self._ensure_strategy(strategy_name)
            delta_notional = max(_coerce_float(notional), 0.0)
            delta_positions = max(_coerce_int(positions), 0)
            if delta_notional:
                entry["current_notional_usdt"] = (
                    entry.get("current_notional_usdt", 0.0) + delta_notional
                )
            if delta_positions:
                entry["current_open_positions"] = (
                    entry.get("current_open_positions", 0) + delta_positions
                )
            self._persist_unlocked()

    def release(
        self,
        strategy_name: str,
        notional: float,
        *,
        positions: int = 1,
    ) -> None:
        with self._lock:
            entry = self._ensure_strategy(strategy_name)
            delta_notional = max(_coerce_float(notional), 0.0)
            delta_positions = max(_coerce_int(positions), 0)
            if delta_notional:
                entry["current_notional_usdt"] = max(
                    entry.get("current_notional_usdt", 0.0) - delta_notional,
                    0.0,
                )
            if delta_positions:
                entry["current_open_positions"] = max(
                    entry.get("current_open_positions", 0) - delta_positions,
                    0,
                )
            self._persist_unlocked()

    def reset_all_usage(self) -> None:
        with self._lock:
            for entry in self._budgets.values():
                entry["current_notional_usdt"] = 0.0
                entry["current_open_positions"] = 0
            self._persist_unlocked()

    def apply_snapshot(
        self, payload: Mapping[str, Mapping[str, object]]
    ) -> dict[str, dict[str, float | int | bool | None]]:
        budgets = self._normalise_budgets(payload)
        if not budgets:
            raise ValueError("budget_snapshot_invalid")
        with self._lock:
            self._budgets = budgets
            self._persist_unlocked()
            return self.snapshot()

    def snapshot(self) -> dict[str, dict[str, float | int | bool | None]]:
        with self._lock:
            result: dict[str, dict[str, float | int | bool | None]] = {}
            for name, entry in self._budgets.items():
                limit_notional = entry.get("max_notional_usdt")
                limit_positions = entry.get("max_open_positions")
                current_notional = entry.get("current_notional_usdt", 0.0)
                current_positions = entry.get("current_open_positions", 0)
                blocked_notional = False
                if limit_notional is not None:
                    blocked_notional = current_notional >= limit_notional - 1e-9
                blocked_positions = False
                if limit_positions is not None:
                    blocked_positions = current_positions >= limit_positions
                result[name] = {
                    "max_notional_usdt": limit_notional,
                    "max_open_positions": limit_positions,
                    "current_notional_usdt": current_notional,
                    "current_open_positions": current_positions,
                    "blocked": bool(blocked_notional or blocked_positions),
                }
            return result


_STRATEGY_BUDGET_MANAGER: StrategyBudgetManager | None = None


def get_strategy_budget_manager() -> StrategyBudgetManager:
    global _STRATEGY_BUDGET_MANAGER
    if _STRATEGY_BUDGET_MANAGER is None:
        _STRATEGY_BUDGET_MANAGER = StrategyBudgetManager()
    return _STRATEGY_BUDGET_MANAGER


def reset_strategy_budget_manager_for_tests(
    manager: StrategyBudgetManager | None = None,
) -> StrategyBudgetManager:
    global _STRATEGY_BUDGET_MANAGER
    if manager is None:
        _STRATEGY_BUDGET_MANAGER = StrategyBudgetManager()
    else:
        _STRATEGY_BUDGET_MANAGER = manager
    return _STRATEGY_BUDGET_MANAGER


__all__ = [
    "StrategyBudgetManager",
    "get_strategy_budget_manager",
    "reset_strategy_budget_manager_for_tests",
]
