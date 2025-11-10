"""Background runner wiring for the reconciliation daemon."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import FastAPI

from ..metrics import RECON_EXCEPTIONS_COUNTER
from ..recon.daemon import ReconDaemon, _resolve_daemon_config
from . import runtime

LOGGER = logging.getLogger(__name__)


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
    """Lightweight wrapper exposing the reconciliation daemon lifecycle."""

    def __init__(self, *, interval: float | None = None) -> None:
        config = _resolve_daemon_config()
        if interval is not None:
            config.interval_sec = interval
        else:
            config.interval_sec = _env_float("RECON_INTERVAL_SEC", config.interval_sec)
        enabled_override = _env_flag("RECON_ENABLED", config.enabled)
        config.enabled = enabled_override
        auto_hold_override = os.getenv("RECON_AUTO_HOLD_ON_CRITICAL")
        if auto_hold_override is not None:
            config.auto_hold_on_critical = _env_flag(
                "RECON_AUTO_HOLD_ON_CRITICAL", config.auto_hold_on_critical
            )
        self._config = config
        self._daemon = ReconDaemon(config)
        self._start_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self._config.enabled:
            LOGGER.info("reconciliation runner disabled by configuration")
            return
        if self._start_task and not self._start_task.done():
            return
        self._start_task = asyncio.create_task(self._daemon.start())
        await asyncio.sleep(0)  # allow start scheduling

    async def stop(self) -> None:
        if self._start_task and not self._start_task.done():
            self._start_task.cancel()
            try:
                await self._start_task
            except asyncio.CancelledError:  # pragma: no cover - lifecycle cleanup
                LOGGER.debug("recon.runner_start_task_cancelled")
        await self._daemon.stop()
        self._start_task = None

    async def run_once(self) -> Any:
        try:
            return await self._daemon.run_once()
        except Exception as exc:
            RECON_EXCEPTIONS_COUNTER.inc()
            runtime.update_reconciliation_status(
                issues=[],
                diffs=[],
                desync_detected=True,
                metadata={"error": str(exc), "status": "ERROR", "state": "ERROR"},
            )
            raise

    @property
    def daemon(self) -> ReconDaemon:
        return self._daemon


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
