"""Service layer utilities for cross-exchange arbitrage."""

from .cross_exchange_arb import check_spread, execute_hedged_trade  # noqa: F401
from .risk_manager import can_open_new_position, register_position  # noqa: F401

__all__ = [
    "check_spread",
    "execute_hedged_trade",
    "can_open_new_position",
    "register_position",
]
