from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import FastAPI

from ..metrics import RECON_DIFFS_GAUGE, RECON_EXCEPTIONS_COUNTER
from ..recon.daemon import ReconDaemon
from . import runtime

LOGGER = logging.getLogger(__name__)

RECON_INTERVAL_SEC = 30.0
RECON_ENABLED = False
RECON_SIGNAL_HOLD = False


def _env_flag(name: str, default: bool) -> bool:
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


class ReconRunner:
    def __init__(
        self,
        _reconciler: Any | None = None,
        *,
        interval: float | None = None,
    ) -> None:
        loop_interval = interval or _env_float(
            "RECON_LOOP_INTERVAL_SEC",
            _env_float("RECON_INTERVAL_SEC", RECON_INTERVAL_SEC),
        )
        self._interval = loop_interval
        self._enabled = _env_flag("RECON_ENABLED", RECON_ENABLED)
        self._signal_hold = _env_flag("ENABLE_RECON_HOLD", RECON_SIGNAL_HOLD)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._daemon: ReconDaemon | None = None

    @property
    def auto_hold_enabled(self) -> bool:
        return self._signal_hold

    @auto_hold_enabled.setter
    def auto_hold_enabled(self, enabled: bool) -> None:
        self._signal_hold = bool(enabled)

    async def start(self) -> None:
        if not self._enabled:
            LOGGER.info("reconciliation runner disabled by configuration")
            return
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
        except asyncio.CancelledError:  # pragma: no cover - lifecycle cleanup
            pass
        finally:
            self._task = None

    async def run_once(self) -> dict[str, object]:
        if self._daemon is None:
            self._daemon = ReconDaemon()
        try:
            snapshots = await self._daemon.run_once()
        except Exception as exc:  # pragma: no cover - defensive logging
            RECON_EXCEPTIONS_COUNTER.inc()
            runtime.update_reconciliation_status(
                diffs=[],
                desync_detected=True,
                metadata={"error": str(exc), "state": "ERROR"},
            )
            raise
        diff_count = len([snapshot for snapshot in snapshots if snapshot.status != "OK"])
        RECON_DIFFS_GAUGE.set(diff_count)
        worst = "OK"
        for snapshot in snapshots:
            status = snapshot.status if isinstance(snapshot.status, str) else "OK"
            if status == "CRITICAL":
                worst = "CRITICAL"
                break
            if status == "WARN" and worst != "CRITICAL":
                worst = "WARN"
        return {
            "snapshots": snapshots,
            "worst_state": worst,
            "auto_hold": self._daemon.auto_hold_active if self._daemon else False,
        }

    async def _run(self) -> None:
        LOGGER.info("reconciliation runner started with interval=%ss", self._interval)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                # run_once already logged and updated metrics; continue loop
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue
        LOGGER.info("reconciliation runner stopped")

_RUNNER = ReconRunner()


def get_runner() -> ReconRunner:
    return _RUNNER


def setup_recon_runner(app: FastAPI) -> None:
    app.state.recon_runner = _RUNNER

    @app.on_event("startup")
    async def _start_runner() -> None:  # pragma: no cover - lifecycle wiring
        await _RUNNER.start()

    @app.on_event("shutdown")
    async def _stop_runner() -> None:  # pragma: no cover - lifecycle wiring
        await _RUNNER.stop()


__all__ = ["ReconRunner", "get_runner", "setup_recon_runner"]
