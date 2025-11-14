from __future__ import annotations

from dataclasses import dataclass
from time import time
from typing import Dict, Optional, Set

from app.alerts.events import evt_readiness
from app.ops.hooks import ops_alert

DEFAULT_REQUIRED_SIGNALS: Set[str] = {"market", "recon", "adapters"}


@dataclass
class Signal:
    ok: bool
    reason: str = ""
    ts: float = 0.0


class HealthAggregator:
    def __init__(self, ttl_seconds: int = 30, required: Optional[Set[str]] = None) -> None:
        self._ttl = int(ttl_seconds)
        self._req: Set[str] = set(required) if required else set(DEFAULT_REQUIRED_SIGNALS)
        self._signals: Dict[str, Signal] = {}
        self._last_ready: Optional[bool] = None

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    @property
    def required(self) -> Set[str]:
        return set(self._req)

    def configure(
        self,
        *,
        ttl_seconds: Optional[int] = None,
        required: Optional[Set[str]] = None,
    ) -> None:
        if ttl_seconds is not None:
            self._ttl = int(ttl_seconds)
        if required is not None:
            self._req = set(required) if required else set(DEFAULT_REQUIRED_SIGNALS)

    def set(self, name: str, ok: bool, *, reason: str = "", now: Optional[float] = None) -> None:
        self._signals[name] = Signal(ok=ok, reason=reason, ts=(now or time()))

    def get(self, name: str) -> Optional[Signal]:
        return self._signals.get(name)

    def clear(self) -> None:
        self._signals.clear()
        self._last_ready = None

    def is_ready(self, now: Optional[float] = None) -> tuple[bool, str]:
        t = now or time()
        missing = []
        bad = []
        for name in sorted(self._req):
            signal = self._signals.get(name)
            if not signal or (t - signal.ts) > self._ttl:
                missing.append(name)
            elif not signal.ok:
                bad.append(f"{name}:{signal.reason or 'fail'}")
        if missing:
            ready = False
            detail = "readiness-missing:" + ",".join(missing)
        elif bad:
            ready = False
            detail = "readiness-bad:" + ",".join(bad)
        else:
            ready = True
            detail = "ok"
        previous = self._last_ready
        self._last_ready = ready
        if previous is True and not ready:
            ops_alert(evt_readiness("bad", detail))
        elif previous is False and ready:
            ops_alert(evt_readiness("ok", "recovered"))
        return ready, detail


_AGG = HealthAggregator()


def get_agg() -> HealthAggregator:
    return _AGG
