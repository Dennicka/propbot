"""Simple risk limit helpers used by the order router."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ..config.trading_profiles import TradingProfile


@dataclass(frozen=True)
class RiskCheckResult:
    allowed: bool
    limit: Decimal
    projected: Decimal


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _normalise_projected(value: Any) -> Decimal:
    return to_decimal(value).copy_abs()


def check_symbol_notional(
    symbol: str, new_notional: Any, profile: TradingProfile
) -> RiskCheckResult:
    limit = to_decimal(profile.max_notional_per_symbol)
    projected = _normalise_projected(new_notional)
    if limit <= 0:
        return RiskCheckResult(True, limit, projected)
    return RiskCheckResult(projected <= limit, limit, projected)


def check_global_notional(new_notional: Any, profile: TradingProfile) -> RiskCheckResult:
    limit = to_decimal(profile.max_notional_global)
    projected = _normalise_projected(new_notional)
    if limit <= 0:
        return RiskCheckResult(True, limit, projected)
    return RiskCheckResult(projected <= limit, limit, projected)


def check_daily_loss(current_loss: Any, profile: TradingProfile) -> RiskCheckResult:
    limit = to_decimal(profile.daily_loss_limit)
    projected = _normalise_projected(current_loss)
    if limit <= 0:
        return RiskCheckResult(True, limit, projected)
    return RiskCheckResult(projected <= limit, limit, projected)


__all__ = [
    "RiskCheckResult",
    "check_symbol_notional",
    "check_global_notional",
    "check_daily_loss",
    "to_decimal",
]
