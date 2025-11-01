from __future__ import annotations

"""Sliding-window risk governor with throttling and auto-hold escalation."""

import math
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Mapping

from ..metrics import (
    increment_risk_window,
    set_risk_error_rate,
    set_risk_success_rate,
    set_risk_throttled,
)
from ..services import runtime
from ..watchdog.core import (
    BrokerStateSnapshot,
    STATE_DEGRADED,
    STATE_DOWN,
    STATE_UP,
    get_broker_state,
)

_ORDER_OK = "ok"
_ORDER_ERROR = "error"

_STATE_ORDER = {STATE_UP: 2, STATE_DEGRADED: 1, STATE_DOWN: 0}
_DEFAULT_WINDOW_SEC = 3600.0
_DEFAULT_MIN_SUCCESS = 0.985
_DEFAULT_MAX_ERROR = 0.01
_DEFAULT_MIN_STATE = STATE_UP
_DEFAULT_HOLD_AFTER = 2


@dataclass(frozen=True)
class RiskDecision:
    throttled: bool
    reason: str | None
    success_rate: float
    error_rate: float
    orders_total: int
    orders_ok: int
    orders_error: int
    window_started_at: float
    auto_hold_reason: str | None = None
    broker_state: str = STATE_UP
    broker_reason: str | None = None


@dataclass
class RiskGovernorConfig:
    window_sec: float = _DEFAULT_WINDOW_SEC
    min_success_rate: float = _DEFAULT_MIN_SUCCESS
    max_order_error_rate: float = _DEFAULT_MAX_ERROR
    min_broker_state: str = _DEFAULT_MIN_STATE
    hold_after_windows: int = _DEFAULT_HOLD_AFTER

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object] | None) -> "RiskGovernorConfig":
        if not isinstance(payload, Mapping):
            return cls()
        try:
            window_sec = float(payload.get("window_sec", _DEFAULT_WINDOW_SEC))
        except (TypeError, ValueError):
            window_sec = _DEFAULT_WINDOW_SEC
        try:
            min_success_rate = float(payload.get("min_success_rate", _DEFAULT_MIN_SUCCESS))
        except (TypeError, ValueError):
            min_success_rate = _DEFAULT_MIN_SUCCESS
        try:
            max_order_error_rate = float(payload.get("max_order_error_rate", _DEFAULT_MAX_ERROR))
        except (TypeError, ValueError):
            max_order_error_rate = _DEFAULT_MAX_ERROR
        min_state = str(payload.get("min_broker_state", _DEFAULT_MIN_STATE) or STATE_UP).upper()
        try:
            hold_after = int(payload.get("hold_after_windows", _DEFAULT_HOLD_AFTER))
        except (TypeError, ValueError):
            hold_after = _DEFAULT_HOLD_AFTER
        if window_sec < 60.0:
            window_sec = 60.0
        if min_success_rate <= 0 or min_success_rate > 1:
            min_success_rate = _DEFAULT_MIN_SUCCESS
        if max_order_error_rate < 0 or max_order_error_rate > 1:
            max_order_error_rate = _DEFAULT_MAX_ERROR
        if min_state not in _STATE_ORDER:
            min_state = _DEFAULT_MIN_STATE
        if hold_after <= 0:
            hold_after = _DEFAULT_HOLD_AFTER
        return cls(
            window_sec=window_sec,
            min_success_rate=min_success_rate,
            max_order_error_rate=max_order_error_rate,
            min_broker_state=min_state,
            hold_after_windows=hold_after,
        )


@dataclass
class _WindowHistoryEntry:
    start: float
    throttled: bool


class SlidingRiskGovernor:
    """Aggregate order outcomes and broker health into throttling decisions."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        config: RiskGovernorConfig | None = None,
    ) -> None:
        self._clock = clock or time.time
        self._config = config or RiskGovernorConfig()
        self._lock = threading.RLock()
        self._events: Deque[tuple[float, str, str]] = deque()
        self._error_breakdown: Counter[str] = Counter()
        self._current_window_start: float | None = None
        self._current_window_throttled: bool = False
        self._window_history: Deque[_WindowHistoryEntry] = deque(maxlen=max(self._config.hold_after_windows + 1, 4))
        self._last_snapshot: Dict[str, object] = {}
        self._last_throttle_reason: str | None = None
        self._last_auto_hold_window: float | None = None

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------
    def record_order_success(self, *, venue: str | None = None, category: str = _ORDER_OK) -> None:
        now = self._clock()
        with self._lock:
            self._events.append((now, _ORDER_OK, _normalise_category(category)))
            self._prune(now)
            if len(self._events) > 16384:
                self._events.popleft()

    def record_order_error(self, *, venue: str | None = None, category: str = _ORDER_ERROR) -> None:
        now = self._clock()
        normalised = _normalise_category(category)
        with self._lock:
            self._events.append((now, _ORDER_ERROR, normalised))
            self._error_breakdown[normalised] += 1
            self._prune(now)
            if len(self._events) > 16384:
                self._events.popleft()

    # ------------------------------------------------------------------
    def compute(self, *, venue: str | None = None) -> RiskDecision:
        now = self._clock()
        snapshot = get_broker_state()
        with self._lock:
            self._prune(now)
            orders_total, orders_ok, orders_error = self._counts()
            success_rate = orders_ok / orders_total if orders_total else 1.0
            error_rate = orders_error / orders_total if orders_total else 0.0
            broker_state, broker_reason = _resolve_broker_state(snapshot, venue)
            reason = self._decide_reason(success_rate, error_rate, broker_state, broker_reason)
            throttled = reason is not None
            auto_hold_reason = self._update_windows(now, throttled, reason)
            decision = RiskDecision(
                throttled=throttled,
                reason=reason,
                success_rate=success_rate,
                error_rate=error_rate,
                orders_total=orders_total,
                orders_ok=orders_ok,
                orders_error=orders_error,
                window_started_at=self._current_window_start or now,
                auto_hold_reason=auto_hold_reason,
                broker_state=broker_state,
                broker_reason=broker_reason,
            )
            self._update_metrics(decision)
            self._store_snapshot(decision, snapshot)
            return decision

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return dict(self._last_snapshot)

    # ------------------------------------------------------------------
    def _prune(self, now: float) -> None:
        window = self._config.window_sec
        threshold = now - window
        while self._events and self._events[0][0] < threshold:
            ts, kind, category = self._events.popleft()
            if kind == _ORDER_ERROR:
                try:
                    self._error_breakdown[category] -= 1
                    if self._error_breakdown[category] <= 0:
                        del self._error_breakdown[category]
                except KeyError:
                    pass

    def _counts(self) -> tuple[int, int, int]:
        total = len(self._events)
        errors = sum(1 for _, kind, _ in self._events if kind == _ORDER_ERROR)
        ok = total - errors
        return total, ok, errors

    def _decide_reason(
        self,
        success_rate: float,
        error_rate: float,
        broker_state: str,
        broker_reason: str | None,
    ) -> str | None:
        if success_rate < self._config.min_success_rate:
            return "LOW_SUCCESS_RATE"
        if error_rate > self._config.max_order_error_rate:
            return "HIGH_ORDER_ERRORS"
        if _STATE_ORDER.get(broker_state, 0) < _STATE_ORDER.get(self._config.min_broker_state, 0):
            if broker_reason:
                return f"BROKER_DEGRADED:{broker_reason}"
            return "BROKER_DEGRADED"
        return None

    def _update_windows(self, now: float, throttled: bool, reason: str | None) -> str | None:
        window_start = math.floor(now / self._config.window_sec) * self._config.window_sec
        auto_hold_reason: str | None = None
        if self._current_window_start is None:
            self._current_window_start = window_start
            self._current_window_throttled = throttled
        elif window_start != self._current_window_start:
            increment_risk_window(self._current_window_throttled)
            self._window_history.append(
                _WindowHistoryEntry(start=self._current_window_start, throttled=self._current_window_throttled)
            )
            self._current_window_start = window_start
            self._current_window_throttled = throttled
        else:
            self._current_window_throttled = self._current_window_throttled or throttled

        history: Deque[_WindowHistoryEntry] = deque(self._window_history, maxlen=self._window_history.maxlen)
        history.append(
            _WindowHistoryEntry(start=self._current_window_start, throttled=self._current_window_throttled)
        )
        if self._config.hold_after_windows > 0 and len(history) >= self._config.hold_after_windows:
            tail = list(history)[-self._config.hold_after_windows :]
            if all(entry.throttled for entry in tail):
                latest_window = tail[-1].start
                if latest_window != self._last_auto_hold_window:
                    auto_hold_reason = f"RISK::{reason or 'UNKNOWN'}"
                    self._last_auto_hold_window = latest_window
        return auto_hold_reason

    def _store_snapshot(self, decision: RiskDecision, snapshot: BrokerStateSnapshot) -> None:
        reason = decision.reason or ""
        if reason.startswith("BROKER_DEGRADED:"):
            reason = "BROKER_DEGRADED"
        payload: Dict[str, object] = {
            "throttled": decision.throttled,
            "reason": decision.reason,
            "success_rate_1h": decision.success_rate,
            "error_rate_1h": decision.error_rate,
            "orders_total": decision.orders_total,
            "orders_ok": decision.orders_ok,
            "orders_error": decision.orders_error,
            "window_started_at": decision.window_started_at,
            "broker_state": decision.broker_state,
            "broker_reason": decision.broker_reason,
            "min_success_rate": self._config.min_success_rate,
            "max_error_rate": self._config.max_order_error_rate,
            "min_broker_state": self._config.min_broker_state,
            "hold_after_windows": self._config.hold_after_windows,
            "window_sec": self._config.window_sec,
            "watchdog": snapshot.as_dict(),
            "error_breakdown": dict(self._error_breakdown),
        }
        self._last_snapshot = payload

    def _update_metrics(self, decision: RiskDecision) -> None:
        set_risk_success_rate(decision.success_rate)
        set_risk_error_rate(decision.error_rate)
        if self._last_throttle_reason and self._last_throttle_reason != decision.reason:
            set_risk_throttled(False, self._last_throttle_reason)
        set_risk_throttled(decision.throttled, decision.reason)
        self._last_throttle_reason = decision.reason


def _normalise_category(category: str) -> str:
    text = (category or "").strip().lower()
    return text or _ORDER_ERROR


def _resolve_broker_state(snapshot: BrokerStateSnapshot, venue: str | None) -> tuple[str, str | None]:
    venue_key = (venue or "").strip().lower() or None
    if venue_key is not None:
        state = snapshot.state_for(venue_key)
    else:
        state = snapshot.overall
    return state.state, state.reason


# ----------------------------------------------------------------------
# Singleton helpers
# ----------------------------------------------------------------------
_GOVERNOR_SINGLETON: SlidingRiskGovernor | None = None
_GOVERNOR_LOCK = threading.RLock()


def configure_risk_governor(*, clock: Callable[[], float] | None = None, config: Mapping[str, object] | None = None) -> None:
    """Initialise the risk governor singleton with the provided configuration."""

    settings = RiskGovernorConfig.from_mapping(config)
    instance = SlidingRiskGovernor(clock=clock, config=settings)
    with _GOVERNOR_LOCK:
        global _GOVERNOR_SINGLETON
        _GOVERNOR_SINGLETON = instance


def get_risk_governor() -> SlidingRiskGovernor:
    with _GOVERNOR_LOCK:
        global _GOVERNOR_SINGLETON
        if _GOVERNOR_SINGLETON is None:
            _GOVERNOR_SINGLETON = SlidingRiskGovernor()
        return _GOVERNOR_SINGLETON


def reset_risk_governor_for_tests() -> None:
    with _GOVERNOR_LOCK:
        global _GOVERNOR_SINGLETON
        _GOVERNOR_SINGLETON = None


# ----------------------------------------------------------------------
# Convenience recorders used by order flows/tests
# ----------------------------------------------------------------------

def record_order_success(*, venue: str | None = None, category: str = _ORDER_OK) -> None:
    governor = get_risk_governor()
    governor.record_order_success(venue=venue, category=category)


def record_order_error(*, venue: str | None = None, category: str = _ORDER_ERROR) -> None:
    governor = get_risk_governor()
    governor.record_order_error(venue=venue, category=category)


def evaluate_pre_trade(*, venue: str | None = None) -> RiskDecision:
    governor = get_risk_governor()
    decision = governor.compute(venue=venue)
    runtime.update_risk_throttle(decision.throttled, reason=decision.reason, source="risk_governor")
    if decision.auto_hold_reason:
        runtime.engage_safety_hold(decision.auto_hold_reason, source="risk_governor")
    state = runtime.get_state()
    safety = getattr(state, "safety", None)
    existing_snapshot: Dict[str, object]
    if safety is not None and isinstance(getattr(safety, "risk_snapshot", None), Mapping):
        existing_snapshot = dict(safety.risk_snapshot)
    else:
        existing_snapshot = {}
    existing_snapshot["governor"] = governor.snapshot()
    runtime.update_risk_snapshot(existing_snapshot)
    return decision


__all__ = [
    "RiskDecision",
    "RiskGovernorConfig",
    "SlidingRiskGovernor",
    "configure_risk_governor",
    "get_risk_governor",
    "reset_risk_governor_for_tests",
    "record_order_success",
    "record_order_error",
    "evaluate_pre_trade",
]
