"""Feature flags for risk-related functionality."""
from __future__ import annotations

import os


def _parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


class FeatureFlags:
    """Container for risk feature flags."""

    RISK_CHECKS_ENABLED: bool = _parse_bool(os.getenv("RISK_CHECKS_ENABLED"))
