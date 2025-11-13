from __future__ import annotations

import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Tuple

DEFAULT_MD_TTL = int(os.getenv("MD_TTL_SEC", "5"))


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        return default
    return max(value, 0)


_STALE_WINDOW = max(_env_int("STALE_P95_WINDOW", 200), 1)


class _TickStore(dict[Tuple[str, str], "TickInfo"]):
    def __init__(self, owner: "Watchdog") -> None:
        super().__init__()
        self._owner = owner

    def clear(self) -> None:  # type: ignore[override]
        super().clear()
        self._owner.clear_samples()


@dataclass
class TickInfo:
    ts: float


@dataclass
class Watchdog:
    ticks: Dict[Tuple[str, str], TickInfo] = field(default_factory=dict)
    staleness_samples: Dict[str, Deque[int]] = field(default_factory=dict)
    cooldown_until: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ticks = _TickStore(self)

    def _venue_key(self, venue: str) -> str:
        return str(venue or "").strip().lower()

    def _buffer_for(self, venue: str) -> Deque[int]:
        key = self._venue_key(venue)
        buffer = self.staleness_samples.get(key)
        if buffer is None:
            buffer = deque(maxlen=_STALE_WINDOW)
            self.staleness_samples[key] = buffer
        return buffer

    def beat(self, venue: str, symbol: str, ts: int | float | None = None) -> None:
        timestamp = float(ts if ts is not None else time.time())
        self.ticks[(venue, symbol)] = TickInfo(ts=timestamp)

    def staleness_ms(self, venue: str, symbol: str, now: float | None = None) -> int | None:
        key = (venue, symbol)
        info = self.ticks.get(key)
        if info is None:
            return None
        now_s = float(now if now is not None else time.time())
        stale_ms = int(max(0.0, (now_s - info.ts) * 1000.0))
        self.note_staleness(venue, stale_ms, now_s)
        return stale_ms

    def note_staleness(self, venue: str, stale_ms: float, now: float | None = None) -> None:
        buffer = self._buffer_for(venue)
        buffer.append(int(max(0.0, stale_ms)))
        if now is not None:
            self.cooldown_active(venue, now=now)

    def is_stale(self, venue: str, symbol: str, now: float | None = None) -> bool:
        stale_ms = self.staleness_ms(venue, symbol, now)
        if stale_ms is None:
            return False  # не знаем — не блокируем
        threshold_ms = max(0, DEFAULT_MD_TTL) * 1000
        return stale_ms > threshold_ms

    def get_p95(self, venue: str) -> int:
        buffer = self.staleness_samples.get(self._venue_key(venue))
        if not buffer:
            return 0
        values = sorted(buffer)
        if not values:
            return 0
        index = max(0, math.ceil(0.95 * len(values)) - 1)
        return int(values[index])

    def stale_p95_limit_ms(self) -> int:
        return _env_int("STALE_P95_LIMIT_MS", 1500)

    def cooldown_seconds(self) -> int:
        return _env_int("STALE_GATE_COOLDOWN_S", 10)

    def activate_cooldown(self, venue: str, now: float | None = None) -> None:
        now_s = float(now if now is not None else time.time())
        self.cooldown_until[self._venue_key(venue)] = now_s + float(self.cooldown_seconds())

    def cooldown_active(self, venue: str, now: float | None = None) -> bool:
        key = self._venue_key(venue)
        expires = self.cooldown_until.get(key)
        if expires is None:
            return False
        now_s = float(now if now is not None else time.time())
        if now_s >= expires:
            self.cooldown_until.pop(key, None)
            return False
        return True

    def clear_samples(self) -> None:
        self.staleness_samples.clear()
        self.cooldown_until.clear()

    def report(self, now: float | None = None) -> dict:
        now_s = float(now if now is not None else time.time())
        out = {}
        threshold_ms = max(0, DEFAULT_MD_TTL) * 1000
        for (venue, symbol), info in self.ticks.items():
            age_s = now_s - info.ts
            out.setdefault(venue, {})[symbol] = {
                "age_s": age_s,
                "stale": age_s * 1000.0 > threshold_ms,
            }
        return out


watchdog = Watchdog()
