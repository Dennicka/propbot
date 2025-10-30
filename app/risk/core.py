"""Risk core scaffold with caps and validation utilities."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


class RiskValidationError(ValueError):
    """Raised when risk limits or inputs are invalid."""


@dataclass(frozen=True)
class RiskCaps:
    """Container for system-wide risk caps.

    The caps are simple positive numeric limits that can be used by other
    services. They intentionally do not contain any orchestration or order
    routing logic – only validation that the configured limits are positive
    numbers.
    """

    max_open_positions: int
    max_total_notional_usdt: float
    max_notional_per_exchange: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_open_positions", self._validate_positive_int("max_open_positions", self.max_open_positions))
        object.__setattr__(self, "max_total_notional_usdt", self._validate_positive_number("max_total_notional_usdt", self.max_total_notional_usdt))
        validated_exchange_caps: Dict[str, float] = {}
        for exchange, cap in self.max_notional_per_exchange.items():
            validated_exchange_caps[exchange] = self._validate_positive_number(
                f"max_notional_per_exchange[{exchange}]", cap
            )
        object.__setattr__(self, "max_notional_per_exchange", validated_exchange_caps)

    @staticmethod
    def _validate_positive_int(name: str, value: int) -> int:
        if value is None or not isinstance(value, int) or value <= 0:
            raise RiskValidationError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _validate_positive_number(name: str, value: float) -> float:
        if value is None:
            raise RiskValidationError(f"{name} must not be None")
        if not isinstance(value, (int, float)):
            raise RiskValidationError(f"{name} must be numeric")
        if value <= 0:
            raise RiskValidationError(f"{name} must be positive")
        return float(value)


class RiskGovernor:
    """Validates exposure metrics against provided :class:`RiskCaps`.

    The governor has no side effects – it merely checks that the supplied
    counts and notionals are non-negative and within the configured limits.
    """

    def __init__(self, caps: RiskCaps) -> None:
        self._caps = caps

    @property
    def caps(self) -> RiskCaps:
        return self._caps

    def ensure_open_positions_within_limit(self, open_positions: int) -> None:
        if open_positions is None or open_positions < 0:
            raise RiskValidationError("open_positions must be a non-negative integer")
        if open_positions > self._caps.max_open_positions:
            raise RiskValidationError(
                f"open_positions {open_positions} exceeds cap {self._caps.max_open_positions}"
            )

    def ensure_total_notional_within_limit(self, total_notional: float) -> None:
        self._ensure_non_negative_number("total_notional", total_notional)
        if total_notional > self._caps.max_total_notional_usdt:
            raise RiskValidationError(
                f"total_notional {total_notional} exceeds cap {self._caps.max_total_notional_usdt}"
            )

    def ensure_exchange_notional_within_limit(self, exchange: str, exchange_notional: float) -> None:
        self._ensure_non_negative_number(f"exchange_notional[{exchange}]", exchange_notional)
        if exchange not in self._caps.max_notional_per_exchange:
            return
        limit = self._caps.max_notional_per_exchange[exchange]
        if exchange_notional > limit:
            raise RiskValidationError(
                f"exchange_notional {exchange_notional} for {exchange} exceeds cap {limit}"
            )

    @staticmethod
    def _ensure_non_negative_number(name: str, value: float) -> None:
        if value is None:
            raise RiskValidationError(f"{name} must not be None")
        if not isinstance(value, (int, float)):
            raise RiskValidationError(f"{name} must be numeric")
        if value < 0:
            raise RiskValidationError(f"{name} must be non-negative")
