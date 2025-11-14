from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal, Optional
import os
import time

HealthLevel = Literal["ok", "warn", "fail"]


@dataclass
class ComponentHealth:
    name: str
    level: HealthLevel
    reason: str
    last_ts: float | None


@dataclass
class HealthSnapshot:
    overall: HealthLevel
    components: Dict[str, ComponentHealth]
    ts: float


class HealthWatchdog:
    def __init__(self) -> None:
        self._router_last_activity: float | None = None
        self._recon_last_run: float | None = None
        self._ledger_last_update: float | None = None

        self._max_router_idle = int(os.environ.get("HEALTH_MAX_ROUTER_IDLE_SEC", "5"))
        self._max_recon_idle = int(os.environ.get("HEALTH_MAX_RECON_IDLE_SEC", "60"))
        self._max_ledger_lag = int(os.environ.get("HEALTH_MAX_LEDGER_LAG_SEC", "30"))

    # --- mark-* API (будут дергать другие компоненты) ---

    def mark_router_activity(self, ts: Optional[float] = None) -> None:
        self._router_last_activity = ts if ts is not None else time.time()

    def mark_recon_run(self, ts: Optional[float] = None) -> None:
        self._recon_last_run = ts if ts is not None else time.time()

    def mark_ledger_update(self, ts: Optional[float] = None) -> None:
        self._ledger_last_update = ts if ts is not None else time.time()

    # --- snapshot ---

    def snapshot(self, now: Optional[float] = None) -> HealthSnapshot:
        t = now if now is not None else time.time()
        components: Dict[str, ComponentHealth] = {}

        def eval_component(name: str, last_ts: float | None, threshold: int) -> ComponentHealth:
            if last_ts is None:
                return ComponentHealth(
                    name=name,
                    level="warn",
                    reason="never-seen",
                    last_ts=None,
                )
            age = t - last_ts
            if age <= threshold:
                return ComponentHealth(name=name, level="ok", reason="", last_ts=last_ts)
            if age <= threshold * 2:
                return ComponentHealth(name=name, level="warn", reason="stale", last_ts=last_ts)
            return ComponentHealth(name=name, level="fail", reason="timeout", last_ts=last_ts)

        components["router"] = eval_component(
            "router", self._router_last_activity, self._max_router_idle
        )
        components["recon"] = eval_component("recon", self._recon_last_run, self._max_recon_idle)
        components["ledger"] = eval_component(
            "ledger", self._ledger_last_update, self._max_ledger_lag
        )

        overall: HealthLevel = "ok"
        levels = [c.level for c in components.values()]
        if "fail" in levels:
            overall = "fail"
        elif "warn" in levels:
            overall = "warn"

        return HealthSnapshot(overall=overall, components=components, ts=t)


# singleton
_WATCHDOG = HealthWatchdog()


def get_watchdog() -> HealthWatchdog:
    return _WATCHDOG
