from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Tuple

ProfileName = Literal["paper", "testnet", "live"]


@dataclass(frozen=True)
class TradingProfile:
    """Represents the runtime trading profile."""

    name: ProfileName
    allow_trading: bool
    strict_flags: bool
    is_canary: bool
    display_name: str


_DEFAULT_PROFILE: ProfileName = "paper"
_VALID_PROFILES: dict[str, ProfileName] = {
    "paper": "paper",
    "testnet": "testnet",
    "live": "live",
}

_TRUTHY = {"1", "true", "yes", "on"}


def _normalise_profile(raw: str | None) -> ProfileName:
    if raw is None:
        return _DEFAULT_PROFILE
    lowered = raw.strip().lower()
    return _VALID_PROFILES.get(lowered, _DEFAULT_PROFILE)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def resolve_canary_state(base_profile: ProfileName) -> Tuple[bool, str]:
    """Return the canary activation flag and display name for ``base_profile``."""

    enabled = _env_flag("CANARY_MODE", False)
    if not enabled:
        return False, base_profile
    override = os.getenv("CANARY_PROFILE_NAME", "").strip()
    display_name = override or f"{base_profile}-canary"
    return True, display_name


def is_canary_mode_enabled() -> bool:
    """Return ``True`` if CANARY_MODE flag is active."""

    return resolve_canary_state(_DEFAULT_PROFILE)[0]


def load_profile_from_env() -> TradingProfile:
    """Load a :class:`TradingProfile` instance from environment variables."""

    profile_name = _normalise_profile(os.getenv("EXEC_PROFILE"))
    canary_active, display_name = resolve_canary_state(profile_name)
    if profile_name == "paper":
        return TradingProfile(
            name="paper",
            allow_trading=True,
            strict_flags=False,
            is_canary=canary_active,
            display_name=display_name if canary_active else "paper",
        )
    if profile_name == "testnet":
        return TradingProfile(
            name="testnet",
            allow_trading=True,
            strict_flags=True,
            is_canary=canary_active,
            display_name=display_name if canary_active else "testnet",
        )
    return TradingProfile(
        name="live",
        allow_trading=True,
        strict_flags=True,
        is_canary=canary_active,
        display_name=display_name if canary_active else "live",
    )


def is_live(profile: TradingProfile) -> bool:
    """Return ``True`` when the provided profile represents live trading."""

    return profile.name == "live"


__all__ = [
    "TradingProfile",
    "load_profile_from_env",
    "is_live",
    "resolve_canary_state",
    "is_canary_mode_enabled",
]
