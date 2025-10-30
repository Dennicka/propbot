"""In-memory exchange watchdog implementation."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, Mapping, MutableMapping, Tuple

from ..metrics import slo

RECENT_TRANSITION_LIMIT = 50


@dataclass(frozen=True)
class WatchdogStateTransition:
    """Capture a watchdog state transition for an exchange."""

    previous: str
    current: str
    reason: str
    auto_hold: bool
    timestamp: float


@dataclass(frozen=True)
class WatchdogCheckResult:
    """Result payload produced by :meth:`ExchangeWatchdog.check_once`."""

    snapshot: Dict[str, Dict[str, Any]]
    transitions: Dict[str, WatchdogStateTransition]


def _coerce_reason(value: object) -> str:
    text = "" if value is None else str(value)
    return text.strip()


class ExchangeWatchdog:
    """Track basic exchange health in memory."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: Dict[str, Dict[str, Any]] = {}
        self._recent_transitions: Deque[tuple[str, WatchdogStateTransition]] = deque(
            maxlen=RECENT_TRANSITION_LIMIT
        )

    def _status_from_entry(self, entry: Mapping[str, Any] | None) -> str:
        if not entry:
            return "UNKNOWN"
        status = str(entry.get("status") or "").strip().upper()
        if status:
            return status
        if "ok" in entry:
            return "OK" if bool(entry.get("ok")) else "DEGRADED"
        return "UNKNOWN"

    def _record_transition(
        self,
        exchange: str,
        transition: WatchdogStateTransition,
    ) -> None:
        self._recent_transitions.append((exchange, transition))

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
        transitions: Dict[str, WatchdogStateTransition] = {}
        with self._lock:
            for exchange, payload in result.items():
                ok, reason = self._normalise_payload(payload)
                current = self._state.get(exchange)
                previous_status = self._status_from_entry(current)
                previous_auto_hold = bool(current.get("auto_hold")) if isinstance(current, Mapping) else False
                auto_hold = previous_auto_hold if not ok else False
                status = "OK" if ok else ("AUTO_HOLD" if auto_hold else "DEGRADED")
                entry = {
                    "ok": ok,
                    "status": status,
                    "auto_hold": auto_hold,
                    "last_check_ts": now,
                    "reason": reason,
                }
                self._state[exchange] = entry
                slo.set_watchdog_ok(exchange, ok)
                if previous_status != status:
                    transition = WatchdogStateTransition(
                        previous=previous_status,
                        current=status,
                        reason=reason,
                        auto_hold=auto_hold,
                        timestamp=now,
                    )
                    transitions[exchange] = transition
                    self._record_transition(exchange, transition)
            snapshot = {name: dict(entry) for name, entry in self._state.items()}
        return WatchdogCheckResult(snapshot=snapshot, transitions=transitions)

    def mark_auto_hold(self, exchange: str, *, reason: str | None = None) -> WatchdogStateTransition | None:
        """Mark ``exchange`` as being under AUTO_HOLD and record the transition."""

        reason_text = _coerce_reason(reason or "")
        now = time.time()
        with self._lock:
            current = self._state.get(exchange)
            if not isinstance(current, Mapping):
                return None
            previous_status = self._status_from_entry(current)
            if current.get("auto_hold"):
                if reason_text and reason_text != current.get("reason"):
                    current = dict(current)
                    current["reason"] = reason_text
                    self._state[exchange] = current
                return None
            entry = dict(current)
            entry["auto_hold"] = True
            entry["status"] = "AUTO_HOLD"
            if reason_text:
                entry["reason"] = reason_text
            entry.setdefault("last_check_ts", now)
            self._state[exchange] = entry
            slo.set_watchdog_ok(exchange, False)
            transition = WatchdogStateTransition(
                previous=previous_status,
                current="AUTO_HOLD",
                reason=entry.get("reason", ""),
                auto_hold=True,
                timestamp=now,
            )
            self._record_transition(exchange, transition)
            return transition

    def get_state(self) -> Dict[str, Dict[str, Any]]:
        """Return a shallow copy of the watchdog state."""

        with self._lock:
            return {name: dict(entry) for name, entry in self._state.items()}

    def get_recent_transitions(self, *, window_minutes: int = RECENT_TRANSITION_LIMIT) -> list[dict[str, Any]]:
        """Return recent transitions within ``window_minutes``."""

        cutoff = time.time() - max(window_minutes, 0) * 60
        with self._lock:
            events = [
                (name, transition)
                for name, transition in self._recent_transitions
                if transition.timestamp >= cutoff
            ]
        result: list[dict[str, Any]] = []
        for name, transition in events[::-1]:
            ts_iso = datetime.fromtimestamp(transition.timestamp, tz=timezone.utc).isoformat()
            result.append(
                {
                    "exchange": name,
                    "previous": transition.previous,
                    "current": transition.current,
                    "reason": transition.reason,
                    "auto_hold": transition.auto_hold,
                    "timestamp": ts_iso,
                }
            )
        return result

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
    "WatchdogStateTransition",
    "WatchdogCheckResult",
    "get_exchange_watchdog",
    "reset_exchange_watchdog_for_tests",
]
