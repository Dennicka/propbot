"""Risk manager for cross-exchange hedges with runtime-backed state."""

from __future__ import annotations

import os
from typing import Dict, Iterable, Tuple

from positions import list_open_positions, reset_positions


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _max_open_positions() -> int:
    return _env_int("MAX_OPEN_POSITIONS", 3)


def _max_notional_per_position() -> float:
    return _env_float("MAX_NOTIONAL_PER_POSITION_USDT", 50_000.0)


def _max_total_notional() -> float:
    return _env_float("MAX_TOTAL_NOTIONAL_USDT", 150_000.0)


def _max_leverage() -> float:
    return _env_float("MAX_LEVERAGE", 5.0)


def _total_open_notional(open_positions: Iterable[Dict[str, object]]) -> float:
    total = 0.0
    for entry in open_positions:
        try:
            total += float(entry.get("notional_usdt", 0.0))
        except (TypeError, ValueError):
            continue
    return total


def can_open_new_position(notion_usdt: float, leverage: float | None = None) -> Tuple[bool, str]:
    """Check whether a new hedge can be opened under the configured limits."""

    if notion_usdt <= 0:
        return False, "invalid_notional"
    max_per_position = _max_notional_per_position()
    if max_per_position > 0 and notion_usdt > max_per_position:
        return False, "per_position_limit_exceeded"
    open_positions = list_open_positions()
    if _max_open_positions() > 0 and len(open_positions) >= _max_open_positions():
        return False, "too_many_open_positions"
    max_total = _max_total_notional()
    if max_total > 0:
        projected_total = _total_open_notional(open_positions) + float(notion_usdt)
        if projected_total > max_total:
            return False, "total_notional_limit_exceeded"
    if leverage is not None and leverage > _max_leverage() > 0:
        return False, "leverage_limit_exceeded"
    return True, ""


def get_open_positions() -> Iterable[Dict[str, object]]:
    return list_open_positions()


__all__ = [
    "can_open_new_position",
    "get_open_positions",
    "reset_positions",
]
