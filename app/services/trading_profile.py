"""Trading profile access helpers."""

from __future__ import annotations

import logging
from threading import RLock
from typing import Mapping

from ..config.trading_profiles import TradingProfile, load_profile


LOGGER = logging.getLogger(__name__)

_PROFILE_LOCK = RLock()
_PROFILE: TradingProfile | None = None


def _serialise_limits(profile: TradingProfile) -> Mapping[str, str]:
    return {
        "max_notional_per_order": str(profile.max_notional_per_order),
        "max_notional_per_symbol": str(profile.max_notional_per_symbol),
        "max_notional_global": str(profile.max_notional_global),
        "daily_loss_limit": str(profile.daily_loss_limit),
        "allow_new_orders": str(profile.allow_new_orders),
        "allow_closures_only": str(profile.allow_closures_only),
    }


def get_trading_profile() -> TradingProfile:
    """Return the cached trading profile for the current process."""

    global _PROFILE
    if _PROFILE is not None:
        return _PROFILE
    with _PROFILE_LOCK:
        if _PROFILE is None:
            profile = load_profile()
            LOGGER.info(
                "trading_profile.loaded",
                extra={
                    "log_module": __name__,
                    "operation": "load_trading_profile",
                    "profile": profile.name,
                    "env_tag": profile.env_tag,
                    "limits": _serialise_limits(profile),
                    "mode": profile.env_tag,
                },
            )
            _PROFILE = profile
    return _PROFILE


def reset_trading_profile_cache() -> None:
    """Reset the cached profile (used by tests)."""

    global _PROFILE
    with _PROFILE_LOCK:
        _PROFILE = None


__all__ = ["get_trading_profile", "reset_trading_profile_cache"]
