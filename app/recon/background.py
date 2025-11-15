from __future__ import annotations

import asyncio
import logging
import os
from typing import Tuple

from fastapi import FastAPI

from app.recon.runner import ReconRunner, ReconRunnerConfig
from app.recon.runner_registry import set_recon_runner

logger = logging.getLogger(__name__)


def _load_recon_runner_settings() -> Tuple[list[str], int]:
    raw = os.getenv("RECON_VENUES") or os.getenv("RECON_RUNNER_VENUES") or ""
    venues = [item.strip() for item in raw.split(",") if item.strip()]
    interval_raw = (
        os.getenv("RECON_INTERVAL_SECONDS")
        or os.getenv("RECON_RUNNER_INTERVAL_SECONDS")
        or os.getenv("RECON_LOOP_INTERVAL_SEC")
        or "60"
    )
    try:
        interval = max(int(float(interval_raw)), 1)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        interval = 60
    return venues, interval


async def _run_recon_forever(runner: ReconRunner, interval_seconds: int) -> None:
    while True:
        try:
            await runner.run_once()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Recon runner iteration failed: %s", exc)
        await asyncio.sleep(max(interval_seconds, 1))


def setup_recon_runner(app: FastAPI) -> None:
    task: asyncio.Task[None] | None = None

    @app.on_event("startup")
    async def _start_recon_runner() -> None:
        nonlocal task
        venues, interval = _load_recon_runner_settings()
        config = ReconRunnerConfig(venues=venues)
        runner = ReconRunner(config=config)
        set_recon_runner(runner)
        if not venues:
            logger.info("Recon runner disabled: no venues configured")
            return
        logger.info("Starting recon runner", extra={"venues": venues, "interval_seconds": interval})
        task = asyncio.create_task(_run_recon_forever(runner, interval))

    @app.on_event("shutdown")
    async def _stop_recon_runner() -> None:
        nonlocal task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:  # pragma: no cover - cancellation path
                logger.debug("Recon runner task cancelled during shutdown")
        task = None


__all__ = ["setup_recon_runner"]
