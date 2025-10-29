from __future__ import annotations

import asyncio
import logging
import os

from fastapi import FastAPI

from ..orchestrator import orchestrator
from ..opsbot import notifier


LOGGER = logging.getLogger(__name__)


def _env_interval() -> float:
    raw = os.getenv("ORCHESTRATOR_ALERT_INTERVAL_SEC")
    if raw is None:
        return 5.0
    try:
        value = float(raw)
    except ValueError:
        return 5.0
    return max(1.0, value)


class _AlertLoop:
    def __init__(self, *, interval: float | None = None) -> None:
        self._interval = interval or _env_interval()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:  # pragma: no cover - lifecycle clean-up
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                orchestrator.emit_alerts_if_needed(notifier)
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.debug("orchestrator alert emission failed: %s", exc, exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue


_ALERT_LOOP = _AlertLoop()


def setup_orchestrator_alerts(app: FastAPI, *, interval: float | None = None) -> None:
    if interval is not None:
        _ALERT_LOOP._interval = max(1.0, float(interval))

    @app.on_event("startup")
    async def _start_loop() -> None:  # pragma: no cover - exercised in integration tests
        await _ALERT_LOOP.start()

    @app.on_event("shutdown")
    async def _stop_loop() -> None:  # pragma: no cover - exercised in integration tests
        await _ALERT_LOOP.stop()


__all__ = ["setup_orchestrator_alerts"]
