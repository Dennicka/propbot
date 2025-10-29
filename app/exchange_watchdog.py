"""Exchange watchdog state manager.

This module provides a lightweight in-memory registry that records health
information for exchange clients.  The watchdog is intentionally decoupled from
runtime orchestration so that other services can poll the current view of the
world without triggering automatic trading stops.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_DEFAULT_RATE_LIMIT_CRITICAL_SEC = 60.0


class ExchangeWatchdog:
    """Track connectivity and throttling state for exchange clients."""

    def __init__(self, *, critical_rate_limit_seconds: float | None = None) -> None:
        self._state: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        if critical_rate_limit_seconds is None:
            critical_rate_limit_seconds = _DEFAULT_RATE_LIMIT_CRITICAL_SEC
        self._critical_rate_limit_seconds = float(critical_rate_limit_seconds)

    def update_from_client(
        self,
        name: str,
        ok: bool,
        rate_limited: bool,
        error: Optional[str] = None,
    ) -> None:
        """Update the recorded state for ``name``.

        ``ok`` signals whether the most recent heartbeat/order test succeeded.
        ``rate_limited`` indicates the client is actively rate limited.
        ``error`` stores a human-readable description of the last failure.
        """

        now = time.time()
        entry = {
            "name": name,
            "reachable": bool(ok),
            "rate_limited": bool(rate_limited),
            "last_ok_ts": None,
            "error": error,
        }
        with self._lock:
            previous = self._state.get(name)
            if previous and previous.get("last_ok_ts") is not None:
                entry["last_ok_ts"] = float(previous["last_ok_ts"])
            if ok:
                entry["last_ok_ts"] = now
            self._state[name] = entry

    def get_state(self) -> Dict[str, Dict[str, Any]]:
        """Return a shallow copy of the current exchange state."""

        with self._lock:
            return {name: data.copy() for name, data in self._state.items()}

    def is_critical(self, name: str) -> bool:
        """Return ``True`` when ``name`` is considered unhealthy."""

        with self._lock:
            entry = self._state.get(name)
            if entry is None:
                return False
            reachable = bool(entry.get("reachable"))
            if not reachable:
                return True
            if not bool(entry.get("rate_limited")):
                return False
            last_ok_ts = entry.get("last_ok_ts")
            if last_ok_ts is None:
                return True
            return (time.time() - float(last_ok_ts)) > self._critical_rate_limit_seconds

    def reset(self) -> None:
        """Clear the internal state.

        Exposed primarily for tests.
        """

        with self._lock:
            self._state.clear()


_watchdog: Optional[ExchangeWatchdog] = None
_watchdog_lock = threading.Lock()


def get_exchange_watchdog() -> ExchangeWatchdog:
    """Return the process-wide watchdog instance."""

    global _watchdog
    if _watchdog is None:
        with _watchdog_lock:
            if _watchdog is None:
                _watchdog = ExchangeWatchdog()
    return _watchdog


def reset_exchange_watchdog_for_tests() -> None:
    """Reset the singleton instance for isolated testing."""

    global _watchdog
    with _watchdog_lock:
        _watchdog = ExchangeWatchdog()


__all__ = ["ExchangeWatchdog", "get_exchange_watchdog", "reset_exchange_watchdog_for_tests"]
