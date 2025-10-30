"""In-memory strategy budget manager for the risk scaffold."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Dict


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch_day(dt: datetime) -> int:
    ts = dt.astimezone(timezone.utc)
    return int(ts.timestamp() // 86400)


class BudgetValidationError(ValueError):
    """Raised when strategy budget inputs are invalid."""


@dataclass
class _BudgetEntry:
    limit_usdt: float | None = None
    used_today_usdt: float = 0.0
    last_reset_epoch_day: int | None = None


class StrategyBudgetManager:
    """Track per-strategy daily loss budgets in memory."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._budgets: Dict[str, _BudgetEntry] = {}

    def set_cap(self, strategy: str, cap: float) -> None:
        strategy_key = self._normalise_strategy(strategy)
        limit = self._validate_positive_number("cap", cap)
        with self._lock:
            entry = self._ensure_entry(strategy_key)
            entry.limit_usdt = limit

    def get_cap(self, strategy: str) -> float | None:
        strategy_key = self._normalise_strategy(strategy)
        with self._lock:
            entry = self._ensure_entry(strategy_key)
            self._maybe_reset(entry)
            return entry.limit_usdt

    def add_usage(self, strategy: str, amount: float) -> dict[str, float | int | None]:
        """Increase the daily usage for ``strategy`` by ``amount`` USDT."""

        strategy_key = self._normalise_strategy(strategy)
        delta = self._validate_positive_number("amount", amount)
        with self._lock:
            entry = self._ensure_entry(strategy_key)
            self._maybe_reset(entry)
            entry.used_today_usdt += delta
            return self._export_entry(entry)

    def reset_usage(self, strategy: str) -> dict[str, float | int | None]:
        strategy_key = self._normalise_strategy(strategy)
        with self._lock:
            entry = self._ensure_entry(strategy_key)
            entry.used_today_usdt = 0.0
            entry.last_reset_epoch_day = _epoch_day(_utc_now())
            return self._export_entry(entry)

    def get_allocation(self, strategy: str) -> float:
        strategy_key = self._normalise_strategy(strategy)
        with self._lock:
            entry = self._ensure_entry(strategy_key)
            self._maybe_reset(entry)
            return entry.used_today_usdt

    def get_remaining(self, strategy: str) -> float | None:
        strategy_key = self._normalise_strategy(strategy)
        with self._lock:
            entry = self._ensure_entry(strategy_key)
            self._maybe_reset(entry)
            if entry.limit_usdt is None:
                return None
            return entry.limit_usdt - entry.used_today_usdt

    def get_budget_state(self, strategy: str) -> dict[str, float | int | None]:
        strategy_key = self._normalise_strategy(strategy)
        with self._lock:
            entry = self._ensure_entry(strategy_key)
            self._maybe_reset(entry)
            return self._export_entry(entry)

    def snapshot(self) -> dict[str, dict[str, float | int | None]]:
        with self._lock:
            snapshot: dict[str, dict[str, float | int | None]] = {}
            for name, entry in self._budgets.items():
                self._maybe_reset(entry)
                snapshot[name] = self._export_entry(entry)
            return snapshot

    def reset_all(self) -> None:
        with self._lock:
            self._budgets.clear()

    def _ensure_entry(self, strategy: str) -> _BudgetEntry:
        entry = self._budgets.get(strategy)
        if entry is None:
            entry = _BudgetEntry(last_reset_epoch_day=_epoch_day(_utc_now()))
            self._budgets[strategy] = entry
        return entry

    def _maybe_reset(self, entry: _BudgetEntry) -> None:
        current_day = _epoch_day(_utc_now())
        if entry.last_reset_epoch_day is None or entry.last_reset_epoch_day != current_day:
            entry.used_today_usdt = 0.0
            entry.last_reset_epoch_day = current_day

    @staticmethod
    def _normalise_strategy(strategy: str) -> str:
        cleaned = str(strategy or "").strip()
        if not cleaned:
            raise BudgetValidationError("strategy must be provided")
        return cleaned

    @staticmethod
    def _validate_positive_number(name: str, value: float) -> float:
        if value is None:
            raise BudgetValidationError(f"{name} must not be None")
        if not isinstance(value, (int, float)):
            raise BudgetValidationError(f"{name} must be numeric")
        if value <= 0:
            raise BudgetValidationError(f"{name} must be positive")
        return float(value)

    @staticmethod
    def _export_entry(entry: _BudgetEntry) -> dict[str, float | int | None]:
        return {
            "limit_usdt": entry.limit_usdt,
            "used_today_usdt": entry.used_today_usdt,
            "last_reset_epoch_day": entry.last_reset_epoch_day,
        }
