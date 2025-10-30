"""Backward-compatible import helpers for the exchange watchdog."""

from __future__ import annotations

from .watchdog.exchange_watchdog import (
    ExchangeWatchdog,
    WatchdogCheckResult,
    WatchdogStateTransition,
    get_exchange_watchdog,
    reset_exchange_watchdog_for_tests,
)

__all__ = [
    "ExchangeWatchdog",
    "WatchdogCheckResult",
    "WatchdogStateTransition",
    "get_exchange_watchdog",
    "reset_exchange_watchdog_for_tests",
]
