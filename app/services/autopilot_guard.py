"""Background guard that disables auto-trading when safety limits are breached."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Mapping

from fastapi import FastAPI

from ..audit_log import log_operator_action
from ..risk.daily_loss import get_daily_loss_cap_state
from ..services.loop import hold_loop
from ..services.runtime import get_state
from ..watchdog.exchange_watchdog import ExchangeWatchdog, get_exchange_watchdog

LOGGER = logging.getLogger(__name__)


DailyLossProvider = Callable[[], Mapping[str, Any]]


def _env_interval() -> float:
    raw = os.getenv("AUTOPILOT_GUARD_INTERVAL_SEC")
    if raw is None:
        return 5.0
    try:
        value = float(raw)
    except ValueError:
        return 5.0
    return max(1.0, value)


class AutopilotGuard:
    """Monitor runtime safety state and disable auto trading when required."""

    def __init__(
        self,
        *,
        interval: float | None = None,
        daily_loss_provider: DailyLossProvider | None = None,
        watchdog: ExchangeWatchdog | None = None,
    ) -> None:
        self._interval = interval or _env_interval()
        self._interval = max(1.0, float(self._interval))
        self._daily_loss_provider = daily_loss_provider or get_daily_loss_cap_state
        self._watchdog = watchdog if watchdog is not None else get_exchange_watchdog()
        self._daily_loss_breached = False
        self._watchdog_status: dict[str, str] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

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
        except asyncio.CancelledError:  # pragma: no cover - lifecycle cleanup
            pass
        finally:
            self._task = None

    async def evaluate_once(self) -> None:
        await self._check_daily_loss()
        await self._check_watchdog()

    async def _run(self) -> None:
        LOGGER.info("autopilot guard loop starting with interval=%ss", self._interval)
        try:
            while not self._stop.is_set():
                try:
                    await self.evaluate_once()
                except Exception:  # pragma: no cover - defensive guard
                    LOGGER.exception("autopilot guard evaluation failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    continue
        finally:
            LOGGER.info("autopilot guard loop stopped")

    async def _check_daily_loss(self) -> None:
        try:
            snapshot_raw = self._daily_loss_provider() or {}
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.exception("daily loss provider failed")
            return
        if not isinstance(snapshot_raw, Mapping):
            LOGGER.debug("daily loss provider returned non-mapping payload: %r", snapshot_raw)
            return
        snapshot = dict(snapshot_raw)
        enabled = bool(snapshot.get("enabled", False))
        blocking = bool(snapshot.get("blocking", False))
        breached_flag = bool(snapshot.get("breached", False))
        active = enabled and blocking
        breached = active and breached_flag
        previous = self._daily_loss_breached
        if breached and not previous:
            await self._trigger_auto_trade_off(
                reason="DAILY_LOSS_BREACH",
                extra={"daily_loss": snapshot},
            )
        self._daily_loss_breached = breached
        if not breached and previous:
            LOGGER.info("autopilot guard: daily loss breach cleared")

    async def _check_watchdog(self) -> None:
        watchdog = self._watchdog
        if watchdog is None:
            return
        try:
            snapshot = watchdog.get_state()
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.exception("failed to read watchdog state")
            return
        if not isinstance(snapshot, Mapping):
            LOGGER.debug("watchdog snapshot not a mapping: %r", snapshot)
            return
        current_status: dict[str, str] = {}
        for exchange, payload in snapshot.items():
            status = "UNKNOWN"
            reason = ""
            if isinstance(payload, Mapping):
                status = str(payload.get("status") or "UNKNOWN").strip().upper() or "UNKNOWN"
                reason = str(payload.get("reason") or "").strip()
            else:
                status = str(payload).strip().upper() or "UNKNOWN"
            previous = self._watchdog_status.get(exchange, "UNKNOWN")
            if status == "AUTO_HOLD" and previous != "AUTO_HOLD":
                await self._trigger_auto_trade_off(
                    reason="WATCHDOG_AUTO_HOLD",
                    extra={"exchange": exchange, "status": status, "watchdog_reason": reason},
                )
            current_status[exchange] = status
        self._watchdog_status = current_status

    async def _trigger_auto_trade_off(
        self,
        *,
        reason: str,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        state = get_state()
        control = getattr(state, "control", None)
        auto_active = bool(getattr(control, "auto_loop", False))
        if not auto_active:
            return
        LOGGER.warning("autopilot guard disabling auto-trade: reason=%s", reason)
        try:
            await hold_loop()
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.exception("failed to disable auto trading via hold_loop")
            return
        state_after = get_state()
        auto_after = bool(getattr(getattr(state_after, "control", None), "auto_loop", False))
        if auto_after:
            LOGGER.warning("autopilot guard attempted to disable auto-trade but it remains active")
            return
        details = {"reason": reason}
        if extra:
            for key, value in extra.items():
                details[str(key)] = value
        log_operator_action("system", "system", "AUTO_TRADE_OFF", details)


_GUARD = AutopilotGuard()


def get_guard() -> AutopilotGuard:
    return _GUARD


def setup_autopilot_guard(app: FastAPI) -> None:
    guard = get_guard()
    app.state.autopilot_guard = guard

    @app.on_event("startup")
    async def _start_guard() -> None:  # pragma: no cover - integration hook
        await guard.start()

    @app.on_event("shutdown")
    async def _stop_guard() -> None:  # pragma: no cover - integration hook
        await guard.stop()


__all__ = ["AutopilotGuard", "get_guard", "setup_autopilot_guard"]
