"""In-memory exchange watchdog implementation."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, MutableMapping, Tuple


@dataclass(frozen=True)
class WatchdogCheckResult:
    """Result payload produced by :meth:`ExchangeWatchdog.check_once`."""

    snapshot: Dict[str, Dict[str, Any]]
    transitions: Dict[str, Tuple[bool, bool]]


def _coerce_reason(value: object) -> str:
    text = "" if value is None else str(value)
    return text.strip()


class ExchangeWatchdog:
    """Track basic exchange health in memory."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: Dict[str, Dict[str, Any]] = {}

    def _normalise_payload(self, payload: object) -> Tuple[bool, str]:
        if isinstance(payload, Mapping):
            ok = bool(payload.get("ok", True))
            reason = _coerce_reason(payload.get("reason", ""))
            return ok, reason
        if isinstance(payload, (tuple, list)):
            if not payload:
                return False, ""
            ok = bool(payload[0])
            reason = _coerce_reason(payload[1] if len(payload) > 1 else "")
            return ok, reason
        if isinstance(payload, Exception):
            return False, _coerce_reason(payload)
        return bool(payload), ""

    def check_once(
        self, probe: Callable[[], Mapping[str, object] | MutableMapping[str, object]]
    ) -> WatchdogCheckResult:
        """Execute ``probe`` and update the in-memory state."""

        result = probe() or {}
        if not isinstance(result, Mapping):
            raise TypeError("probe must return a mapping of exchange -> status")
        result = dict(result)

        now = time.time()
        transitions: Dict[str, Tuple[bool, bool]] = {}
        with self._lock:
            for exchange, payload in result.items():
                ok, reason = self._normalise_payload(payload)
                current = self._state.get(exchange)
                previous_ok = current.get("ok") if isinstance(current, Mapping) else None
                if previous_ok is None and not ok:
                    transitions[exchange] = (True, False)
                elif previous_ok is not None and bool(previous_ok) != ok:
                    transitions[exchange] = (bool(previous_ok), ok)
                self._state[exchange] = {
                    "ok": ok,
                    "last_check_ts": now,
                    "reason": reason,
                }
            snapshot = {name: dict(entry) for name, entry in self._state.items()}
        return WatchdogCheckResult(snapshot=snapshot, transitions=transitions)

    def get_state(self) -> Dict[str, Dict[str, Any]]:
        """Return a shallow copy of the watchdog state."""

        with self._lock:
            return {name: dict(entry) for name, entry in self._state.items()}

    def overall_ok(self) -> bool:
        """Return ``True`` when all tracked exchanges report healthy status."""

        with self._lock:
            if not self._state:
                return True
            return all(bool(entry.get("ok", False)) for entry in self._state.values())

    def failing_exchanges(self) -> Dict[str, Dict[str, Any]]:
        """Return a snapshot of exchanges currently failing health checks."""

        with self._lock:
            return {
                name: dict(entry)
                for name, entry in self._state.items()
                if not bool(entry.get("ok", False))
            }

    def most_recent_failure(self) -> Tuple[str, Dict[str, Any]] | None:
        """Return the most recent failing exchange entry, if any."""

        failing = self.failing_exchanges()
        if not failing:
            return None
        latest = max(
            failing.items(),
            key=lambda item: float(item[1].get("last_check_ts", 0.0)),
        )
        return latest


_watchdog: ExchangeWatchdog | None = None
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
    """Reset the global watchdog instance. Intended for tests."""

    global _watchdog
    with _watchdog_lock:
        _watchdog = ExchangeWatchdog()


__all__ = [
    "ExchangeWatchdog",
    "WatchdogCheckResult",
    "get_exchange_watchdog",
    "reset_exchange_watchdog_for_tests",
]
