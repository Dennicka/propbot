from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List

from fastapi import FastAPI

from .metrics import slo_snapshot

LOGGER = logging.getLogger(__name__)

__all__ = ["SLOEvaluation", "SLOMonitor", "evaluate", "setup_slo_monitor"]


@dataclass
class SLOEvaluation:
    ok: bool
    breaches: List[str]
    snapshot: Dict[str, Any]


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


async def _emit_breach(message: str) -> None:
    try:
        from ..opsbot import notifier

        notifier.alert_slo_breach(message)
    except Exception:  # pragma: no cover - notifier failures are logged elsewhere
        LOGGER.debug("failed to emit SLO notifier alert", exc_info=True)
    try:
        from ..telebot import telegram_bot

        await telegram_bot.alert_slo_breach(message)
    except Exception:  # pragma: no cover - telegram failures should not break loop
        LOGGER.debug("failed to emit Telegram SLO alert", exc_info=True)


async def evaluate(
    latency_target_ms: float,
    error_rate_target: float,
    *,
    notify: bool = True,
) -> SLOEvaluation:
    snapshot = slo_snapshot()
    breaches: List[str] = []

    ui = snapshot.get("ui", {})
    ui_p95 = ui.get("p95_ms") if isinstance(ui, dict) else None
    if isinstance(ui_p95, (int, float)) and ui_p95 > latency_target_ms:
        breaches.append(f"UI p95={ui_p95:.1f}ms > target {latency_target_ms:.1f}ms")

    core = snapshot.get("core", {}) if isinstance(snapshot, dict) else {}
    core_p95_values: List[float] = []
    if isinstance(core, dict):
        for stats in core.values():
            if isinstance(stats, dict):
                value = stats.get("p95_ms")
                if isinstance(value, (int, float)):
                    core_p95_values.append(float(value))
    if core_p95_values:
        worst_core_p95 = max(core_p95_values)
        if worst_core_p95 > latency_target_ms:
            breaches.append(
                f"Core p95={worst_core_p95:.1f}ms > target {latency_target_ms:.1f}ms"
            )

    overall = snapshot.get("overall", {}) if isinstance(snapshot, dict) else {}
    error_rate = overall.get("error_rate") if isinstance(overall, dict) else None
    if isinstance(error_rate, (int, float)) and error_rate > error_rate_target:
        breaches.append(f"error_rate={error_rate:.4f} > target {error_rate_target:.4f}")

    ok = not breaches
    evaluation = SLOEvaluation(ok=ok, breaches=breaches, snapshot=snapshot)
    if notify and breaches:
        message = "; ".join(breaches)
        await _emit_breach(f"SLO breach detected: {message}")
    return evaluation


def _feature_enabled() -> bool:
    return _env_flag("FEATURE_SLO", False)


def _interval_seconds(default: float = 60.0) -> float:
    value = _env_float("SLO_EVALUATION_INTERVAL_SEC", default)
    return max(5.0, value)


class SLOMonitor:
    def __init__(
        self,
        *,
        interval: float | None = None,
        latency_target_ms: float | None = None,
        error_rate_target: float | None = None,
    ) -> None:
        self._interval = interval or _interval_seconds()
        self._latency_target = latency_target_ms or _env_float(
            "SLO_LATENCY_P95_TARGET_MS", 250.0
        )
        self._error_rate_target = error_rate_target or _env_float(
            "SLO_ERROR_RATE_TARGET", 0.02
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_ok = True

    async def start(self) -> None:
        if not _feature_enabled():
            LOGGER.info("SLO monitor disabled via FEATURE_SLO=0")
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="slo-monitor")

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:  # pragma: no cover - lifecycle cleanup
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = await evaluate(
                    self._latency_target,
                    self._error_rate_target,
                    notify=self._last_ok,
                )
                self._last_ok = result.ok
            except asyncio.CancelledError:  # pragma: no cover - propagation
                raise
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.exception("SLO evaluation failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue


_MONITOR = SLOMonitor()


def setup_slo_monitor(app: FastAPI, *, interval: float | None = None) -> None:
    if interval is not None:
        _MONITOR._interval = max(5.0, float(interval))

    app.state.slo_monitor = _MONITOR

    @app.on_event("startup")
    async def _start_monitor() -> None:  # pragma: no cover - exercised in integration tests
        await _MONITOR.start()

    @app.on_event("shutdown")
    async def _stop_monitor() -> None:  # pragma: no cover - exercised in integration tests
        await _MONITOR.stop()
