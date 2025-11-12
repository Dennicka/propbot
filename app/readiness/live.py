from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, Literal, Tuple

StatusT = Literal["ready", "degraded", "down"]

DEFAULT_TTL = int(os.getenv("READINESS_TTL_SEC", "15"))
CRITICAL = {"router", "market_data", "risk"}


@dataclass
class Probe:
    name: str
    ts: int


@dataclass
class Registry:
    items: Dict[str, Probe] = field(default_factory=dict)

    def beat(self, name: str, ts: int | None = None) -> None:
        self.items[name] = Probe(name=name, ts=int(ts or time.time()))

    def report(self, now: int | None = None) -> Tuple[StatusT, Dict[str, Dict]]:
        now_s = int(now or time.time())
        components: Dict[str, Dict] = {}
        any_stale = False
        any_down = False
        for name, probe in self.items.items():
            age = now_s - probe.ts
            stale = age > DEFAULT_TTL
            components[name] = {"age_s": age, "stale": stale}
            if stale:
                any_stale = True
                if name in CRITICAL:
                    any_down = True
        if any_down:
            return "down", components
        if any_stale:
            return "degraded", components
        return "ready", components


registry = Registry()
