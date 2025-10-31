"""Journal helpers and feature flag detection."""
from __future__ import annotations

import os
from typing import Final

_FALSEY: Final[set[str]] = {"", "0", "false", "off", "no", "disable", "disabled"}


def is_enabled(default: bool = False) -> bool:
    """Return True when the journal feature flag is active."""
    value = os.getenv("FEATURE_JOURNAL")
    if value is None:
        return default
    return value.strip().lower() not in _FALSEY


__all__ = ["is_enabled"]
