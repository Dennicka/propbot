"""Simple in-memory risk manager for cross-exchange hedges."""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

MAX_NOTIONAL_USDT = float(os.getenv("ARB_MAX_NOTIONAL_USDT", "50000"))
MAX_OPEN_POSITIONS = int(os.getenv("ARB_MAX_OPEN_POSITIONS", "3"))
MAX_LEVERAGE = float(os.getenv("ARB_MAX_LEVERAGE", "5"))

open_positions: List[Dict[str, object]] = []


def can_open_new_position(notion_usdt: float, leverage: float) -> Tuple[bool, str]:
    if notion_usdt > MAX_NOTIONAL_USDT:
        return False, "notional_limit_exceeded"
    if leverage > MAX_LEVERAGE:
        return False, "leverage_limit_exceeded"
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        return False, "too_many_open_positions"
    return True, ""


def register_position(position_dict: Dict[str, object]) -> None:
    open_positions.append(position_dict)


def get_open_positions() -> List[Dict[str, object]]:
    return list(open_positions)


def reset_positions() -> None:
    open_positions.clear()
