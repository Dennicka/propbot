from __future__ import annotations

import os
from enum import Enum
from typing import Dict


class AlertLevel(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @classmethod
    def coerce(cls, value: "AlertLevel | str") -> "AlertLevel":
        if isinstance(value, cls):
            return value
        try:
            return cls[str(value).strip().upper()]
        except KeyError as exc:  # pragma: no cover - defensive
            raise ValueError(f"Unknown alert level: {value!r}") from exc


def _normalise_profile(profile: str | None) -> str:
    if not profile:
        profile = os.getenv("DEFAULT_PROFILE", "paper")
    profile = profile.strip().lower()
    if profile in {"testnet", "live"}:
        return profile
    return "paper"


def should_route(level: "AlertLevel | str", profile: str | None = None) -> Dict[str, bool]:
    resolved = AlertLevel.coerce(level)
    current_profile = _normalise_profile(profile)

    telegram = False
    if resolved in {AlertLevel.WARN, AlertLevel.ERROR, AlertLevel.CRITICAL}:
        if current_profile in {"testnet", "live"}:
            telegram = True
    if resolved is AlertLevel.CRITICAL:
        telegram = True

    return {"stdout": True, "logfile": True, "telegram": telegram}


__all__ = ["AlertLevel", "should_route"]
