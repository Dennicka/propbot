"""Exchange watchdog state manager.

This module exposes an in-memory registry that records health information for
exchange clients.  The goal is to provide a single source of truth for
operator-facing dashboards and monitoring without forcing every caller to track
its own view of exchange connectivity.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Mapping, Optional

_DEFAULT_RATE_LIMIT_CRITICAL_SEC = 60.0
_DEFAULT_EXCHANGES = ("binance", "okx")


def _build_default_entry() -> Dict[str, Any]:
    return {
        "reachable": True,
        "rate_limited": False,
        "last_ok_ts": 0.0,
        "error": "",
    }


class ExchangeWatchdog:
    """Track connectivity and throttling state for exchange clients."""

    def __init__(
        self,
        *,
        critical_rate_limit_seconds: float | None = None,
        exchanges: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        if critical_rate_limit_seconds is None:
            critical_rate_limit_seconds = _DEFAULT_RATE_LIMIT_CRITICAL_SEC
        self._critical_rate_limit_seconds = float(critical_rate_limit_seconds)
        self._lock = threading.RLock()
        self._state: Dict[str, Dict[str, Any]] = {}
        self._rate_limited_since: Dict[str, float | None] = {}

        initial = dict(exchanges or {})
        for name in _DEFAULT_EXCHANGES:
            if name not in initial:
                initial[name] = {}
        for name, payload in initial.items():
            self._state[name] = self._normalise_entry(payload)
            self._rate_limited_since[name] = None

    def _normalise_entry(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        entry = _build_default_entry()
        if "reachable" in payload:
            entry["reachable"] = bool(payload.get("reachable"))
        if "rate_limited" in payload:
            entry["rate_limited"] = bool(payload.get("rate_limited"))
        if "last_ok_ts" in payload:
            try:
                entry["last_ok_ts"] = float(payload.get("last_ok_ts", 0.0))
            except (TypeError, ValueError):
                entry["last_ok_ts"] = 0.0
        if "error" in payload and payload.get("error") is not None:
            entry["error"] = str(payload.get("error", ""))
        return entry

    def _ensure_entry(self, name: str) -> Dict[str, Any]:
        if name not in self._state:
            self._state[name] = _build_default_entry()
            self._rate_limited_since[name] = None
        return self._state[name]

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

        timestamp = time.time()
        normalised_error = "" if error is None else str(error)
        with self._lock:
            entry = self._ensure_entry(name)
            entry["reachable"] = bool(ok)
            entry["rate_limited"] = bool(rate_limited)
            entry["error"] = normalised_error
            if ok:
                entry["last_ok_ts"] = float(timestamp)
            if rate_limited:
                since = self._rate_limited_since.get(name)
                if since is None:
                    self._rate_limited_since[name] = float(timestamp)
            else:
                self._rate_limited_since[name] = None

    def get_state(self) -> Dict[str, Dict[str, Any]]:
        """Return a snapshot of the current exchange state."""

        with self._lock:
            return {name: data.copy() for name, data in self._state.items()}

    def is_critical(self, name: str) -> bool:
        """Return ``True`` when ``name`` is considered unhealthy."""

        now = time.time()
        with self._lock:
            entry = self._state.get(name)
            if entry is None:
                return False
            if not bool(entry.get("reachable")):
                return True
            if not bool(entry.get("rate_limited")):
                return False
            since = self._rate_limited_since.get(name)
            if since is None:
                # Rate limiting just started. Give it time to recover.
                self._rate_limited_since[name] = now
                return False
            return (now - float(since)) >= self._critical_rate_limit_seconds

    def reset(self) -> None:
        """Clear the internal state.

        Exposed primarily for tests.
        """

        with self._lock:
            known = set(self._state) | set(_DEFAULT_EXCHANGES)
            self._state = {name: _build_default_entry() for name in known}
            for name in known:
                self._rate_limited_since[name] = None


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
