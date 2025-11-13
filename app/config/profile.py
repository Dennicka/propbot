from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

ProfileName = Literal["paper", "testnet", "live"]


@dataclass(frozen=True)
class TradingProfile:
    """Represents the runtime trading profile."""

    name: ProfileName
    allow_trading: bool
    strict_flags: bool


_DEFAULT_PROFILE: ProfileName = "paper"
_VALID_PROFILES: dict[str, ProfileName] = {
    "paper": "paper",
    "testnet": "testnet",
    "live": "live",
}


def _normalise_profile(raw: str | None) -> ProfileName:
    if raw is None:
        return _DEFAULT_PROFILE
    lowered = raw.strip().lower()
    return _VALID_PROFILES.get(lowered, _DEFAULT_PROFILE)


def load_profile_from_env() -> TradingProfile:
    """Load a :class:`TradingProfile` instance from environment variables."""

    profile_name = _normalise_profile(os.getenv("EXEC_PROFILE"))
    if profile_name == "paper":
        return TradingProfile(name="paper", allow_trading=True, strict_flags=False)
    if profile_name == "testnet":
        return TradingProfile(name="testnet", allow_trading=True, strict_flags=True)
    return TradingProfile(name="live", allow_trading=True, strict_flags=True)


def is_live(profile: TradingProfile) -> bool:
    """Return ``True`` when the provided profile represents live trading."""

    return profile.name == "live"


__all__ = ["TradingProfile", "load_profile_from_env", "is_live"]
