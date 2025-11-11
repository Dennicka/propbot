"""Execution trading profiles and risk limit configuration."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import logging
import os
from enum import Enum


LOGGER = logging.getLogger(__name__)


class ExecutionProfile(str, Enum):
    """Supported execution environments."""

    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


@dataclass(frozen=True)
class TradingProfile:
    """Runtime risk controls for a trading environment."""

    name: str
    max_notional_per_order: Decimal
    max_notional_per_symbol: Decimal
    max_notional_global: Decimal
    daily_loss_limit: Decimal
    env_tag: str
    allow_new_orders: bool
    allow_closures_only: bool


_PROFILE_LIMITS: dict[ExecutionProfile, TradingProfile] = {
    ExecutionProfile.PAPER: TradingProfile(
        name="paper",
        max_notional_per_order=Decimal("1000"),
        max_notional_per_symbol=Decimal("5000"),
        max_notional_global=Decimal("20000"),
        daily_loss_limit=Decimal("5000"),
        env_tag="paper",
        allow_new_orders=True,
        allow_closures_only=False,
    ),
    ExecutionProfile.TESTNET: TradingProfile(
        name="testnet",
        max_notional_per_order=Decimal("5000"),
        max_notional_per_symbol=Decimal("25000"),
        max_notional_global=Decimal("100000"),
        daily_loss_limit=Decimal("20000"),
        env_tag="testnet",
        allow_new_orders=True,
        allow_closures_only=False,
    ),
    ExecutionProfile.LIVE: TradingProfile(
        name="live",
        max_notional_per_order=Decimal("100000"),
        max_notional_per_symbol=Decimal("500000"),
        max_notional_global=Decimal("5000000"),
        daily_loss_limit=Decimal("250000"),
        env_tag="live",
        allow_new_orders=True,
        allow_closures_only=False,
    ),
}


class TradingProfileError(RuntimeError):
    """Raised when TRADING_PROFILE contains an unknown value."""


def _resolve_profile_name(raw: str | None) -> ExecutionProfile:
    if not raw:
        return ExecutionProfile.PAPER
    text = raw.strip().lower()
    for profile in ExecutionProfile:
        if profile.value == text:
            return profile
    raise TradingProfileError(f"Unsupported trading profile: {raw!r}")


def load_profile() -> TradingProfile:
    """Load trading profile from ``TRADING_PROFILE`` env variable."""

    raw = os.getenv("TRADING_PROFILE")
    try:
        profile_key = _resolve_profile_name(raw)
    except TradingProfileError as exc:
        LOGGER.warning("trading_profile.invalid", extra={"profile": raw, "error": str(exc)})
        profile_key = ExecutionProfile.PAPER
    return _PROFILE_LIMITS[profile_key]


__all__ = [
    "ExecutionProfile",
    "TradingProfile",
    "TradingProfileError",
    "load_profile",
]
