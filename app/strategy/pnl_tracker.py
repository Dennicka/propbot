from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Iterable

_ONE_DAY = 24 * 60 * 60
_ROLLING_WINDOW = 7 * _ONE_DAY
_RETENTION_WINDOW = _ROLLING_WINDOW + _ONE_DAY


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


@dataclass
class _PnlEvent:
    ts: float
    pnl: float
    simulated: bool = False


class StrategyPnlTracker:
    """In-memory ring-buffer tracker for per-strategy realised PnL."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: Dict[str, Deque[_PnlEvent]] = {}

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def _events_for(self, strategy: str) -> Deque[_PnlEvent]:
        key = strategy.strip()
        if not key:
            raise ValueError("strategy must be non-empty")
        if key not in self._events:
            self._events[key] = deque()
        return self._events[key]

    def _prune(self, events: Deque[_PnlEvent], cutoff: float) -> None:
        while events and events[0].ts < cutoff:
            events.popleft()

    def record_fill(
        self,
        strategy: str,
        realized_pnl: float,
        ts: float | None = None,
        *,
        simulated: bool = False,
    ) -> None:
        """Record a realised fill for ``strategy`` with optional timestamp."""

        timestamp = float(ts if ts is not None else time.time())
        pnl_value = float(realized_pnl or 0.0)
        event = _PnlEvent(ts=timestamp, pnl=pnl_value, simulated=bool(simulated))
        now = time.time()
        cutoff = max(timestamp, now) - _RETENTION_WINDOW
        with self._lock:
            events = self._events_for(strategy)
            events.append(event)
            self._prune(events, cutoff)

    def exclude_simulated_entries(self) -> bool:
        return _env_flag("EXCLUDE_DRY_RUN_FROM_PNL", True)

    def snapshot(self, *, exclude_simulated: bool | None = None) -> dict[str, dict[str, float]]:
        """Return per-strategy aggregates for today/7d realised PnL."""

        now = time.time()
        today_cutoff = now - _ONE_DAY
        window_cutoff = now - _ROLLING_WINDOW
        if exclude_simulated is None:
            exclude_simulated = self.exclude_simulated_entries()
        else:
            exclude_simulated = bool(exclude_simulated)

        with self._lock:
            data = {name: list(events) for name, events in self._events.items() if events}

        result: dict[str, dict[str, float]] = {}
        for name, events in data.items():
            filtered: Iterable[_PnlEvent]
            if exclude_simulated:
                filtered = [event for event in events if not event.simulated]
            else:
                filtered = list(events)
            filtered_list = list(filtered)
            if exclude_simulated and not filtered_list:
                continue

            realized_today = sum(event.pnl for event in filtered_list if event.ts >= today_cutoff)
            realized_7d = sum(event.pnl for event in filtered_list if event.ts >= window_cutoff)
            drawdown = self._max_drawdown(filtered_list, window_cutoff)
            result[name] = {
                "realized_today": float(realized_today),
                "realized_7d": float(realized_7d),
                "max_drawdown_7d": float(drawdown),
            }
        return result

    @staticmethod
    def _max_drawdown(events: Iterable[_PnlEvent], cutoff: float) -> float:
        window_events = [event for event in events if event.ts >= cutoff]
        if not window_events:
            return 0.0
        window_events.sort(key=lambda event: event.ts)
        running = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for event in window_events:
            running += event.pnl
            if running > peak:
                peak = running
            else:
                drawdown = peak - running
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
        return max_drawdown


_tracker: StrategyPnlTracker | None = None
_tracker_lock = threading.Lock()


def get_strategy_pnl_tracker() -> StrategyPnlTracker:
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = StrategyPnlTracker()
    return _tracker


def reset_strategy_pnl_tracker_for_tests() -> None:
    tracker = get_strategy_pnl_tracker()
    tracker.reset()
