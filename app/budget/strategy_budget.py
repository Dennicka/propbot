"""In-memory strategy budget manager for the risk scaffold."""
from __future__ import annotations

from typing import Dict


class BudgetValidationError(ValueError):
    """Raised when strategy budget inputs are invalid."""


class StrategyBudgetManager:
    """Simple per-strategy budget caps held entirely in memory."""

    def __init__(self) -> None:
        self._caps: Dict[str, float] = {}
        self._allocations: Dict[str, float] = {}

    def set_cap(self, strategy: str, cap: float) -> None:
        if not strategy:
            raise BudgetValidationError("strategy must be provided")
        self._caps[strategy] = self._validate_positive_number("cap", cap)
        # Reset allocation if it is now above the cap
        current = self._allocations.get(strategy, 0.0)
        if current > self._caps[strategy]:
            self._allocations[strategy] = self._caps[strategy]

    def allocate(self, strategy: str, amount: float) -> None:
        amount = self._validate_positive_number("amount", amount)
        cap = self._caps.get(strategy)
        if cap is None:
            raise BudgetValidationError(f"No cap configured for strategy {strategy}")
        current = self._allocations.get(strategy, 0.0)
        if current + amount > cap:
            raise BudgetValidationError(
                f"Allocation {current + amount} exceeds cap {cap} for strategy {strategy}"
            )
        self._allocations[strategy] = current + amount

    def release(self, strategy: str, amount: float) -> None:
        amount = self._validate_positive_number("amount", amount)
        current = self._allocations.get(strategy, 0.0)
        if amount > current:
            raise BudgetValidationError(
                f"Cannot release {amount}; only {current} allocated for strategy {strategy}"
            )
        self._allocations[strategy] = current - amount

    def get_cap(self, strategy: str) -> float | None:
        return self._caps.get(strategy)

    def get_allocation(self, strategy: str) -> float:
        return self._allocations.get(strategy, 0.0)

    def get_remaining(self, strategy: str) -> float | None:
        cap = self._caps.get(strategy)
        if cap is None:
            return None
        return cap - self._allocations.get(strategy, 0.0)

    @staticmethod
    def _validate_positive_number(name: str, value: float) -> float:
        if value is None:
            raise BudgetValidationError(f"{name} must not be None")
        if not isinstance(value, (int, float)):
            raise BudgetValidationError(f"{name} must be numeric")
        if value <= 0:
            raise BudgetValidationError(f"{name} must be positive")
        return float(value)
