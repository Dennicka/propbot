from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, Tuple

DEFAULT_MD_TTL = int(os.getenv("MD_TTL_SEC", "5"))


@dataclass
class TickInfo:
    ts: int


@dataclass
class Watchdog:
    ticks: Dict[Tuple[str, str], TickInfo] = field(default_factory=dict)

    def beat(self, venue: str, symbol: str, ts: int | None = None) -> None:
        self.ticks[(venue, symbol)] = TickInfo(ts=int(ts or time.time()))

    def is_stale(self, venue: str, symbol: str, now: int | None = None) -> bool:
        key = (venue, symbol)
        if key not in self.ticks:
            return False  # не знаем — не блокируем
        now_s = int(now or time.time())
        return (now_s - self.ticks[key].ts) > DEFAULT_MD_TTL

    def report(self, now: int | None = None) -> dict:
        now_s = int(now or time.time())
        out = {}
        for (venue, symbol), info in self.ticks.items():
            out.setdefault(venue, {})[symbol] = {
                "age_s": now_s - info.ts,
                "stale": (now_s - info.ts) > DEFAULT_MD_TTL,
            }
        return out


watchdog = Watchdog()
