import os

from .profile import normalise_profile_category

TRUE_SET = {"1", "true", "on", "yes", "y", "True", "TRUE", "ON"}


def _env_on(name: str) -> bool | None:
    v = os.getenv(name)
    return (v in TRUE_SET) if v is not None else None


def pretrade_strict_on() -> bool:
    v = _env_on("FF_PRETRADE_STRICT")
    if v is not None:
        return v
    raw = os.getenv("DEFAULT_PROFILE")
    if not raw:
        return False
    prof = normalise_profile_category(raw)
    return prof in {"paper", "testnet"}


def risk_limits_on() -> bool:
    v = _env_on("FF_RISK_LIMITS")
    if v is not None:
        return v
    raw = os.getenv("DEFAULT_PROFILE")
    if not raw:
        return False
    prof = normalise_profile_category(raw)
    return prof in {"paper", "testnet"}


def md_watchdog_on() -> bool:
    v = _env_on("FF_MD_WATCHDOG")
    if v is not None:
        return v
    raw = os.getenv("DEFAULT_PROFILE")
    if not raw:
        return False
    prof = normalise_profile_category(raw)
    return prof in {"paper", "testnet"}
