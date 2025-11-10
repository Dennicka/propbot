"""Broker watchdog with error-budget guardrails."""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Iterable, Mapping

from ..metrics import set_watchdog_state_metric as metrics_set_state_metric
from ..metrics.broker_watchdog import (
    increment_disconnect as metrics_increment_disconnect,
    record_auto_hold as metrics_record_auto_hold,
    set_state as metrics_set_state,
    update_metrics as metrics_update_metrics,
)


LOGGER = logging.getLogger(__name__)

STATE_OK = "OK"
STATE_DEGRADED = "DEGRADED"
STATE_DOWN = "DOWN"

_DEFAULT_THRESHOLDS: Mapping[str, Mapping[str, float]] = {
    "ws_lag_ms_p95": {"degraded": 400.0, "down": 1200.0},
    "ws_disconnects_per_min": {"degraded": 2.0, "down": 6.0},
    "rest_5xx_rate": {"degraded": 0.02, "down": 0.10},
    "rest_timeouts_rate": {"degraded": 0.02, "down": 0.10},
    "order_reject_rate": {"degraded": 0.01, "down": 0.05},
}

_LAG_WINDOW_S = 120.0
_RATE_WINDOW_S = 60.0
_REST_WINDOW_S = 300.0
_ORDER_WINDOW_S = 300.0
_HISTORY_WINDOW_MULTIPLIER = 3


@dataclass
class _VenueState:
    ws_lag_samples: Deque[tuple[float, float]] = field(default_factory=deque)
    ws_disconnects: Deque[float] = field(default_factory=deque)
    rest_total: Deque[float] = field(default_factory=deque)
    rest_5xx: Deque[float] = field(default_factory=deque)
    rest_timeouts: Deque[float] = field(default_factory=deque)
    order_total: Deque[float] = field(default_factory=deque)
    order_rejects: Deque[tuple[float, str]] = field(default_factory=deque)
    order_reject_codes: Counter[str] = field(default_factory=Counter)
    state: str = STATE_OK
    last_reason: str = ""
    burn_rate: float = 0.0
    last_transition_ts: float = 0.0
    history: Deque[tuple[float, str]] = field(default_factory=deque)

    def record_state(self, ts: float, state: str) -> None:
        self.last_transition_ts = ts
        self.history.append((ts, state))


class BrokerWatchdog:
    """Aggregate broker health signals and emit guardrail decisions."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        thresholds: Mapping[str, Mapping[str, float]] | None = None,
        error_budget_window_s: float = 600.0,
        auto_hold_on_down: bool = True,
        block_on_down: bool = True,
        event_queue: "asyncio.Queue[Mapping[str, object]] | None" = None,
        on_throttle_change: Callable[[bool, str | None], None] | None = None,
        on_auto_hold: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._clock = clock or time.time
        self._thresholds = self._normalise_thresholds(thresholds)
        self._error_budget_window = max(float(error_budget_window_s), 60.0)
        self._auto_hold_on_down = bool(auto_hold_on_down)
        self._block_on_down = bool(block_on_down)
        self._event_queue = event_queue
        self._on_throttle_change = on_throttle_change
        self._on_auto_hold = on_auto_hold
        self._lock = threading.RLock()
        self._venues: Dict[str, _VenueState] = {}
        self._throttled: Dict[str, str] = {}
        self._last_reason: str = ""

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------
    def record_ws_lag(self, venue: str, lag_ms: float) -> None:
        now = self._clock()
        with self._lock:
            state = self._ensure_state(venue)
            state.ws_lag_samples.append((now, float(lag_ms)))
            self._evaluate(venue, state, now)

    def record_ws_disconnect(self, venue: str) -> None:
        now = self._clock()
        with self._lock:
            state = self._ensure_state(venue)
            state.ws_disconnects.append(now)
            metrics_increment_disconnect(self._canonical(venue))
            self._evaluate(venue, state, now, reason_hint="ws_disconnect_spike")

    def record_rest_ok(self, venue: str) -> None:
        now = self._clock()
        with self._lock:
            state = self._ensure_state(venue)
            state.rest_total.append(now)
            self._evaluate(venue, state, now)

    def record_rest_error(self, venue: str, kind: str | None = None) -> None:
        now = self._clock()
        normalised = (kind or "").strip().lower()
        with self._lock:
            state = self._ensure_state(venue)
            state.rest_total.append(now)
            if normalised in {"timeout", "timeouts", "timed_out"}:
                state.rest_timeouts.append(now)
            else:
                state.rest_5xx.append(now)
            reason = "rest_timeout_spike" if normalised.startswith("timeout") else "rest_5xx_spike"
            self._evaluate(venue, state, now, reason_hint=reason)

    def record_order_submit(self, venue: str) -> None:
        now = self._clock()
        with self._lock:
            state = self._ensure_state(venue)
            state.order_total.append(now)
            self._evaluate(venue, state, now)

    def record_order_reject(self, venue: str, code: str | None = None) -> None:
        now = self._clock()
        reject_code = (code or "UNKNOWN").strip().upper() or "UNKNOWN"
        with self._lock:
            state = self._ensure_state(venue)
            state.order_total.append(now)
            state.order_rejects.append((now, reject_code))
            state.order_reject_codes[reject_code] += 1
            self._evaluate(venue, state, now, reason_hint=f"order_reject:{reject_code}")

    # ------------------------------------------------------------------
    # Public state helpers
    # ------------------------------------------------------------------
    def state_for(self, venue: str) -> str:
        with self._lock:
            state = self._venues.get(self._canonical(venue))
            return state.state if state else STATE_OK

    def snapshot(self) -> Dict[str, object]:
        now = self._clock()
        with self._lock:
            per_venue: Dict[str, Dict[str, object]] = {}
            for venue, state in self._venues.items():
                metrics = self._collect_metrics(state, now)
                per_venue[venue] = {
                    "state": state.state,
                    "ws_lag_ms_p95": metrics["ws_lag_ms_p95"],
                    "ws_disconnects_per_min": metrics["ws_disconnects_per_min"],
                    "rest_5xx_rate": metrics["rest_5xx_rate"],
                    "rest_timeouts_rate": metrics["rest_timeouts_rate"],
                    "order_reject_rate": metrics["order_reject_rate"],
                    "order_reject_breakdown": dict(state.order_reject_codes),
                    "burn_rate": state.burn_rate,
                    "last_reason": state.last_reason,
                    "updated_ts": state.last_transition_ts or now,
                }
            throttled = bool(self._throttled)
            snapshot = {
                "per_venue": per_venue,
                "last_reason": self._last_reason,
                "throttled": throttled,
            }
        return snapshot

    def throttled(self) -> bool:
        with self._lock:
            return bool(self._throttled)

    def should_block_orders(self, venue: str) -> bool:
        state = self.state_for(venue)
        if state == STATE_DOWN and self._block_on_down:
            return True
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_state(self, venue: str) -> _VenueState:
        key = self._canonical(venue)
        state = self._venues.get(key)
        if state is None:
            state = _VenueState()
            state.record_state(self._clock(), STATE_OK)
            self._venues[key] = state
        return state

    def _canonical(self, venue: str) -> str:
        text = (venue or "").strip().lower()
        return text or "unknown"

    def _normalise_thresholds(
        self, thresholds: Mapping[str, Mapping[str, float]] | None
    ) -> Dict[str, Dict[str, float]]:
        if not thresholds:
            return {metric: dict(levels) for metric, levels in _DEFAULT_THRESHOLDS.items()}
        normalised: Dict[str, Dict[str, float]] = {}
        for metric, payload in thresholds.items():
            degraded = float(payload.get("degraded", 0.0))
            down = float(payload.get("down", degraded))
            if down < degraded:
                down = degraded
            normalised[metric] = {"degraded": degraded, "down": down}
        for metric, payload in _DEFAULT_THRESHOLDS.items():
            normalised.setdefault(metric, dict(payload))
        return normalised

    def _evaluate(
        self,
        venue: str,
        state: _VenueState,
        now: float,
        *,
        reason_hint: str | None = None,
    ) -> None:
        metrics = self._collect_metrics(state, now)
        label = self._canonical(venue)
        metrics_update_metrics(
            label,
            ws_lag_ms_p95=metrics["ws_lag_ms_p95"],
            rest_5xx_rate=metrics["rest_5xx_rate"],
            rest_timeouts_rate=metrics["rest_timeouts_rate"],
            order_reject_rate=metrics["order_reject_rate"],
        )
        reason = reason_hint or ""
        severity = STATE_OK
        for metric_name, value in metrics.items():
            limits = self._thresholds.get(metric_name, {})
            degraded = limits.get("degraded", math.inf)
            down = limits.get("down", math.inf)
            if value >= down and down > 0:
                severity = STATE_DOWN
                reason = reason or f"{metric_name}_spike"
                break
            if value >= degraded and degraded > 0 and severity != STATE_DOWN:
                severity = STATE_DEGRADED
                reason = reason or f"{metric_name}_elevated"
        burn_rate = self._compute_burn_rate(state, severity, now)
        if severity == STATE_OK and burn_rate > 1.0:
            severity = STATE_DEGRADED
            reason = reason or "error_budget_exhausted"
        self._apply_state(venue, state, severity, burn_rate, reason or state.last_reason, now)
        metrics_set_state_metric(label, state.state)

    def _collect_metrics(self, state: _VenueState, now: float) -> Dict[str, float]:
        self._prune_deque(state.ws_lag_samples, now, _LAG_WINDOW_S)
        self._prune_deque(state.ws_disconnects, now, _RATE_WINDOW_S)
        self._prune_deque(state.rest_total, now, _REST_WINDOW_S)
        self._prune_deque(state.rest_5xx, now, _REST_WINDOW_S)
        self._prune_deque(state.rest_timeouts, now, _REST_WINDOW_S)
        self._prune_deque(state.order_total, now, _ORDER_WINDOW_S)

        def _remove_reject(item: tuple[float, str]) -> None:
            _, code = item
            current = state.order_reject_codes.get(code, 0)
            if current <= 1:
                state.order_reject_codes.pop(code, None)
            else:
                state.order_reject_codes[code] = current - 1

        self._prune_deque(state.order_rejects, now, _ORDER_WINDOW_S, on_remove=_remove_reject)
        lag_values = [sample for _, sample in state.ws_lag_samples]
        ws_lag = self._percentile(lag_values, 95)
        disconnect_rate = self._rate_per_minute(len(state.ws_disconnects), _RATE_WINDOW_S)
        rest_total = len(state.rest_total) or 0
        rest_5xx = len(state.rest_5xx) or 0
        rest_timeouts = len(state.rest_timeouts) or 0
        rest_5xx_rate = (rest_5xx / rest_total) if rest_total else 0.0
        rest_timeout_rate = (rest_timeouts / rest_total) if rest_total else 0.0
        order_total = len(state.order_total) or 0
        order_rejects = len(state.order_rejects) or 0
        order_reject_rate = (order_rejects / order_total) if order_total else 0.0
        return {
            "ws_lag_ms_p95": ws_lag,
            "ws_disconnects_per_min": disconnect_rate,
            "rest_5xx_rate": rest_5xx_rate,
            "rest_timeouts_rate": rest_timeout_rate,
            "order_reject_rate": order_reject_rate,
        }

    def _prune_deque(
        self,
        dq: Deque,
        now: float,
        window: float,
        *,
        on_remove: Callable[[object], None] | None = None,
    ) -> None:
        cutoff = now - window
        while dq:
            head = dq[0]
            ts = head[0] if isinstance(head, tuple) else head
            if ts >= cutoff:
                break
            item = dq.popleft()
            if on_remove is not None:
                try:
                    on_remove(item)
                except Exception as exc:  # pragma: no cover - defensive  # noqa: BLE001
                    LOGGER.debug(
                        "broker watchdog prune callback failed",
                        extra={"window": window},
                        exc_info=exc,
                    )

    def _percentile(self, values: Iterable[float], percentile: float) -> float:
        cleaned = [float(v) for v in values if v is not None]
        if not cleaned:
            return 0.0
        ordered = sorted(cleaned)
        k = (len(ordered) - 1) * (percentile / 100.0)
        f = math.floor(k)
        c = math.ceil(k)
        if f == c:
            return ordered[int(k)]
        d0 = ordered[int(f)] * (c - k)
        d1 = ordered[int(c)] * (k - f)
        return d0 + d1

    def _rate_per_minute(self, count: int, window: float) -> float:
        if window <= 0:
            return float(count)
        return float(count) * 60.0 / window

    def _compute_burn_rate(self, state: _VenueState, severity: str, now: float) -> float:
        window = self._error_budget_window
        cutoff = now - window
        history_window = window * _HISTORY_WINDOW_MULTIPLIER
        while state.history and state.history[0][0] < now - history_window:
            state.history.popleft()
        effective_history = list(state.history)
        if not effective_history or effective_history[-1][1] != severity:
            effective_history.append((now, severity))
        total_bad = 0.0
        prev_ts = now
        prev_state = severity
        for ts, entry_state in reversed(effective_history):
            if ts < cutoff:
                ts = cutoff
            duration = max(prev_ts - ts, 0.0)
            if prev_state in {STATE_DEGRADED, STATE_DOWN}:
                total_bad += duration
            prev_ts = ts
            prev_state = entry_state
            if ts <= cutoff:
                break
        burn_rate = total_bad / window if window > 0 else 0.0
        state.burn_rate = burn_rate
        return burn_rate

    def _apply_state(
        self,
        venue: str,
        state: _VenueState,
        severity: str,
        burn_rate: float,
        reason: str,
        now: float,
    ) -> None:
        previous = state.state
        state.state = severity
        state.last_reason = reason
        if not state.history or state.history[-1][1] != severity:
            state.record_state(now, severity)
        if previous != severity:
            self._handle_transition(venue, severity, reason, now)
        self._update_throttle(venue, severity, reason)
        metrics_set_state(self._canonical(venue), severity)
        if (
            severity == STATE_DOWN
            and previous != STATE_DOWN
            and self._auto_hold_on_down
            and self._on_auto_hold
        ):
            self._on_auto_hold(venue, severity, reason)
            metrics_record_auto_hold(self._canonical(venue))

    def _handle_transition(self, venue: str, state: str, reason: str, ts: float) -> None:
        self._last_reason = f"{venue}:{state}:{reason}".strip(":")
        if self._event_queue is not None:
            try:
                self._event_queue.put_nowait(
                    {
                        "venue": venue,
                        "state": state,
                        "reason": reason,
                        "ts": ts,
                    }
                )
            except Exception as exc:  # pragma: no cover - defensive  # noqa: BLE001
                LOGGER.debug(
                    "broker watchdog event queue publish failed",
                    extra={"venue": venue, "state": state},
                    exc_info=exc,
                )

    def _update_throttle(self, venue: str, state: str, reason: str) -> None:
        throttled_before = bool(self._throttled)
        if state in {STATE_DEGRADED, STATE_DOWN}:
            self._throttled[venue] = reason
        else:
            self._throttled.pop(venue, None)
        throttled_after = bool(self._throttled)
        if throttled_before != throttled_after and self._on_throttle_change:
            aggregate_reason: str | None = None
            if throttled_after:
                active_venue, active_reason = next(iter(self._throttled.items()))
                if active_reason:
                    aggregate_reason = f"{active_venue}:{active_reason}"
                else:
                    aggregate_reason = active_venue
            self._on_throttle_change(throttled_after, aggregate_reason)


_watchdog_instance: BrokerWatchdog | None = None
_watchdog_lock = threading.Lock()


def configure_broker_watchdog(
    *,
    clock: Callable[[], float] | None = None,
    thresholds: Mapping[str, Mapping[str, float]] | None = None,
    error_budget_window_s: float = 600.0,
    auto_hold_on_down: bool = True,
    block_on_down: bool = True,
    event_queue: "asyncio.Queue[Mapping[str, object]] | None" = None,
    on_throttle_change: Callable[[bool, str | None], None] | None = None,
    on_auto_hold: Callable[[str, str, str], None] | None = None,
) -> BrokerWatchdog:
    """Create and configure the process-wide watchdog instance."""

    global _watchdog_instance
    with _watchdog_lock:
        _watchdog_instance = BrokerWatchdog(
            clock=clock,
            thresholds=thresholds,
            error_budget_window_s=error_budget_window_s,
            auto_hold_on_down=auto_hold_on_down,
            block_on_down=block_on_down,
            event_queue=event_queue,
            on_throttle_change=on_throttle_change,
            on_auto_hold=on_auto_hold,
        )
        return _watchdog_instance


def get_broker_watchdog() -> BrokerWatchdog:
    """Return the configured broker watchdog (creating a default one if needed)."""

    global _watchdog_instance
    if _watchdog_instance is None:
        with _watchdog_lock:
            if _watchdog_instance is None:
                _watchdog_instance = BrokerWatchdog()
    return _watchdog_instance


def reset_broker_watchdog_for_tests() -> None:
    """Reset the broker watchdog instance for test isolation."""

    global _watchdog_instance
    with _watchdog_lock:
        _watchdog_instance = BrokerWatchdog()


__all__ = [
    "BrokerWatchdog",
    "STATE_OK",
    "STATE_DEGRADED",
    "STATE_DOWN",
    "configure_broker_watchdog",
    "get_broker_watchdog",
    "reset_broker_watchdog_for_tests",
]
