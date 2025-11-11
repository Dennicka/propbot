"""Runaway cancel guard with per-venue/per-symbol counters."""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Mapping

RUNAWAY_GUARD_V2_SOURCE = "runaway_guard_v2"

_WINDOW_SECONDS = 60


@dataclass
class _BlockDetails:
    reason: str
    venue: str
    symbol: str
    count: int
    limit: int
    cooldown_remaining: float = 0.0

    def as_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "reason": self.reason,
            "venue": self.venue,
            "symbol": self.symbol,
            "count": self.count,
            "limit": self.limit,
        }
        if self.cooldown_remaining > 0:
            payload["cooldown_remaining"] = round(self.cooldown_remaining, 2)
        return payload


class RunawayGuard:
    """Track cancel bursts per venue/symbol and enforce cooldowns."""

    def __init__(self) -> None:
        self._max_cancels_per_min = 0
        self._cooldown_sec = 0
        self._enabled = False
        self._lock = threading.Lock()
        self._per_venue: Dict[str, Dict[str, Deque[float]]] = defaultdict(
            lambda: defaultdict(deque)
        )
        self._last_trigger_ts: float | None = None
        self._last_block: _BlockDetails | None = None

    @staticmethod
    def feature_enabled() -> bool:
        raw = os.getenv("FEATURE_RUNAWAY_GUARD_V2")
        if raw is None:
            return False
        raw = raw.strip().lower()
        return raw not in {"", "0", "false", "no", "off"}

    def configure(self, *, max_cancels_per_min: int, cooldown_sec: int) -> None:
        with self._lock:
            self._max_cancels_per_min = max(0, int(max_cancels_per_min))
            self._cooldown_sec = max(0, int(cooldown_sec))
            self._enabled = self._max_cancels_per_min > 0
            self._per_venue.clear()
            self._last_trigger_ts = None
            self._last_block = None

    def allow_cancel(
        self, venue: str, symbol: str, *, planned: int = 1, now: float | None = None
    ) -> bool:
        if not self.feature_enabled():
            return True
        with self._lock:
            if not self._enabled or planned <= 0:
                return True
            now = time.time() if now is None else now
            venue_key = (venue or "").lower()
            symbol_key = (symbol or "").upper()
            queue = self._per_venue[venue_key][symbol_key]
            self._prune(queue, now)
            limit = self._max_cancels_per_min
            if limit <= 0:
                return True
            last_trigger = self._last_trigger_ts or 0.0
            cooldown_remaining = last_trigger + self._cooldown_sec - now
            if self._cooldown_sec > 0 and cooldown_remaining > 0:
                self._last_block = _BlockDetails(
                    reason="cooldown_active",
                    venue=venue_key,
                    symbol=symbol_key,
                    count=len(queue),
                    limit=limit,
                    cooldown_remaining=cooldown_remaining,
                )
                return False
            projected = len(queue) + planned
            if projected > limit:
                self._last_trigger_ts = now
                self._last_block = _BlockDetails(
                    reason="limit_exceeded",
                    venue=venue_key,
                    symbol=symbol_key,
                    count=projected,
                    limit=limit,
                )
                return False
            return True

    def register_cancel(
        self, venue: str, symbol: str, *, count: int = 1, now: float | None = None
    ) -> None:
        if not self.feature_enabled():
            return
        if count <= 0:
            return
        with self._lock:
            if not self._enabled:
                return
            now = time.time() if now is None else now
            venue_key = (venue or "").lower()
            symbol_key = (symbol or "").upper()
            queue = self._per_venue[venue_key][symbol_key]
            self._prune(queue, now)
            for _ in range(count):
                queue.append(now)

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            per_venue: Dict[str, Dict[str, int]] = {}
            now = time.time()
            for venue, per_symbol in self._per_venue.items():
                symbol_counts: Dict[str, int] = {}
                for symbol, queue in per_symbol.items():
                    self._prune(queue, now)
                    symbol_counts[symbol] = len(queue)
                if symbol_counts:
                    per_venue[venue] = symbol_counts
            last_trigger_iso = None
            if self._last_trigger_ts:
                last_trigger_iso = time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.gmtime(self._last_trigger_ts)
                )
            payload: Dict[str, object] = {
                "enabled": self._enabled and self.feature_enabled(),
                "max_cancels_per_min": self._max_cancels_per_min,
                "cooldown_sec": self._cooldown_sec,
                "per_venue": per_venue,
                "last_trigger_ts": last_trigger_iso,
            }
            if self._last_block:
                payload["last_block"] = self._last_block.as_dict()
            return payload

    def last_block(self) -> Mapping[str, object] | None:
        with self._lock:
            return self._last_block.as_dict() if self._last_block else None

    def _prune(self, queue: Deque[float], now: float) -> None:
        boundary = now - _WINDOW_SECONDS
        while queue and queue[0] < boundary:
            queue.popleft()


def get_guard() -> RunawayGuard:
    return _GUARD


_GUARD = RunawayGuard()


class RunawayGuardCooldownError(RuntimeError):
    """Raised when runaway guard cooldown is still in effect."""

    def __init__(self, details: Mapping[str, object] | None = None) -> None:
        super().__init__("runaway_guard_cooldown")
        self.details = dict(details) if isinstance(details, Mapping) else {}
