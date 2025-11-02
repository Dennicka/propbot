from __future__ import annotations

"""Foundational utilities for resilient websocket market data streams."""

import enum
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable

from app.metrics.market_ws import (
    WS_CONNECT_TOTAL,
    WS_DISCONNECT_TOTAL,
    WS_GAP_DETECTED_TOTAL,
    WS_RESYNC_TOTAL,
)

logger = logging.getLogger(__name__)


class WsState(str, enum.Enum):
    """Lifecycle state of a websocket market data stream."""

    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RESYNCING = "RESYNCING"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


@dataclass(slots=True)
class BackoffPolicy:
    """Exponential backoff controller with jitter and stability reset."""

    base: float = 0.25
    maximum: float = 30.0
    stable_window: float = 60.0
    jitter: Callable[[float, float], float] = random.uniform
    clock: Callable[[], float] = time.monotonic

    _attempt: int = 0
    _stable_since: float | None = None

    def next_delay(self) -> float:
        self._attempt += 1
        exponential = self.base * (2 ** (self._attempt - 1))
        capped = min(self.maximum, exponential)
        low = max(self.base, capped)
        high = max(self.base, capped * 1.5)
        delay = max(self.base, float(self.jitter(low, high)))
        logger.debug(
            "backoff.next_delay", extra={"attempt": self._attempt, "computed": delay}
        )
        return delay

    def record_failure(self) -> None:
        self._stable_since = None

    def record_success(self) -> None:
        now = self.clock()
        if self._attempt == 0:
            return
        if self._stable_since is None:
            self._stable_since = now
            return
        if now - self._stable_since >= self.stable_window:
            self.reset()

    def reset(self) -> None:
        logger.debug("backoff.reset", extra={"attempt": self._attempt})
        self._attempt = 0
        self._stable_since = None


@dataclass(slots=True)
class HeartbeatMonitor:
    """Tracks message activity and triggers reconnect on silence."""

    timeout: float
    clock: Callable[[], float] = time.monotonic

    _last_seen: float | None = None

    def mark_seen(self, ts: float | None = None) -> None:
        if ts is None:
            ts = self.clock()
        self._last_seen = float(ts)

    def stale_for(self) -> float:
        if self._last_seen is None:
            return 0.0
        return max(self.clock() - self._last_seen, 0.0)

    def is_expired(self) -> bool:
        if self.timeout <= 0:
            return False
        if self._last_seen is None:
            return False
        expired = self.clock() - self._last_seen > self.timeout
        if expired:
            logger.warning(
                "ws.heartbeat.timeout",
                extra={"timeout": self.timeout, "last_seen": self._last_seen},
            )
        return expired

    def reset(self) -> None:
        self._last_seen = None


@dataclass(slots=True)
class GapEvent:
    venue: str
    symbol: str
    last_seq: int | None
    expected: int | None
    got_from: int


class GapDetector:
    """Detects sequence gaps and out-of-order diff events."""

    def __init__(self, *, venue: str, symbol: str) -> None:
        self.venue = venue
        self.symbol = symbol
        self._expected: int | None = None
        self._lock = threading.Lock()

    def reset(self, last_seq: int | None) -> None:
        with self._lock:
            self._expected = None if last_seq is None else last_seq + 1

    def observe(self, seq_from: int, seq_to: int) -> GapEvent | None:
        with self._lock:
            expected = self._expected
            if expected is not None and seq_from != expected:
                event = GapEvent(
                    venue=self.venue,
                    symbol=self.symbol,
                    last_seq=expected - 1 if expected is not None else None,
                    expected=expected,
                    got_from=seq_from,
                )
                logger.warning(
                    "ws.gap.detected",
                    extra={
                        "venue": self.venue,
                        "symbol": self.symbol,
                        "expected": expected,
                        "seq_from": seq_from,
                        "seq_to": seq_to,
                    },
                )
                WS_GAP_DETECTED_TOTAL.labels(venue=self.venue, symbol=self.symbol).inc()
                return event
            self._expected = seq_to + 1
        return None


class WsConnector:
    """High level connection manager with backoff and heartbeat supervision."""

    def __init__(
        self,
        *,
        venue: str,
        heartbeat: HeartbeatMonitor,
        backoff: BackoffPolicy,
        reconnect: Callable[[str], None],
    ) -> None:
        self.venue = venue
        self._heartbeat = heartbeat
        self._backoff = backoff
        self._reconnect_cb = reconnect
        self.state = WsState.DOWN
        self.last_reason: str = ""
        self._lock = threading.Lock()

    def transition(self, state: WsState, *, reason: str | None = None) -> None:
        with self._lock:
            if self.state == state and not reason:
                return
            logger.info(
                "ws.state.transition",
                extra={"venue": self.venue, "from": self.state, "to": state, "reason": reason},
            )
            self.state = state
            if reason:
                self.last_reason = reason

    def on_open(self) -> None:
        WS_CONNECT_TOTAL.labels(venue=self.venue).inc()
        self._backoff.reset()
        self.transition(WsState.CONNECTED, reason="connected")

    def on_disconnect(self, *, reason: str) -> float:
        WS_DISCONNECT_TOTAL.labels(venue=self.venue, reason=reason).inc()
        self.transition(WsState.DOWN, reason=reason)
        self._backoff.record_failure()
        delay = self._backoff.next_delay()
        logger.info(
            "ws.backoff.schedule",
            extra={"venue": self.venue, "reason": reason, "sleep_s": delay},
        )
        return delay

    def on_message(self, *, ts: float | None = None) -> None:
        self._heartbeat.mark_seen(ts)
        self._backoff.record_success()

    def check_heartbeat(self) -> None:
        if not self._heartbeat.is_expired():
            return
        reason = "heartbeat_timeout"
        delay = self.on_disconnect(reason=reason)
        self._reconnect_cb(reason)
        logger.info(
            "ws.reconnect.heartbeat",
            extra={"venue": self.venue, "reason": reason, "sleep_s": delay},
        )

    def mark_resync(self, symbol: str) -> None:
        WS_RESYNC_TOTAL.labels(venue=self.venue, symbol=symbol).inc()

    def reconnect_now(self, reason: str) -> None:
        self.on_disconnect(reason=reason)
        self._reconnect_cb(reason)


__all__ = [
    "BackoffPolicy",
    "GapDetector",
    "GapEvent",
    "HeartbeatMonitor",
    "WsConnector",
    "WsState",
]
