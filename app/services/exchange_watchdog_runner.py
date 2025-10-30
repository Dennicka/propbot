from __future__ import annotations

import asyncio
import inspect
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping

from fastapi import FastAPI

from ..opsbot import notifier
from . import runtime
from ..watchdog.exchange_watchdog import (
    ExchangeWatchdog,
    WatchdogCheckResult,
    WatchdogStateTransition,
    get_exchange_watchdog,
)

LOGGER = logging.getLogger(__name__)

WatchdogProbe = Callable[[], Mapping[str, object] | Awaitable[Mapping[str, object]]]


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_interval() -> float:
    raw = os.getenv("WATCHDOG_INTERVAL_SEC")
    if raw is None:
        return 7.0
    try:
        value = float(raw)
    except ValueError:
        return 7.0
    return max(1.0, value)


class ExchangeWatchdogRunner:
    def __init__(
        self,
        watchdog: ExchangeWatchdog,
        *,
        probe: WatchdogProbe | None = None,
        interval: float | None = None,
    ) -> None:
        self._watchdog = watchdog
        self._probe: WatchdogProbe = probe or (lambda: {})
        self._interval = interval or _env_interval()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_overall_ok = True

    def set_probe(self, probe: WatchdogProbe) -> None:
        self._probe = probe

    async def start(self) -> None:
        if not _env_flag("WATCHDOG_ENABLED"):
            LOGGER.info("exchange watchdog disabled by configuration")
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

    async def check_once(self) -> WatchdogCheckResult | None:
        try:
            result = self._probe()
            if inspect.isawaitable(result):
                result = await result  # type: ignore[assignment]
            assert not inspect.isawaitable(result)
            report = self._watchdog.check_once(lambda: result)
        except Exception as exc:  # pragma: no cover - defensive guard
            LOGGER.warning("exchange watchdog probe failed: %s", exc, exc_info=True)
            return None
        self._handle_transitions(report)
        await self._apply_policies(report)
        return report

    async def _run(self) -> None:
        LOGGER.info(
            "exchange watchdog loop starting with interval=%ss", self._interval
        )
        while not self._stop.is_set():
            await self.check_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue
        LOGGER.info("exchange watchdog loop stopped")

    def _maybe_emit_transition_alert(
        self, exchange: str, transition: WatchdogStateTransition
    ) -> None:
        if not _env_flag("NOTIFY_WATCHDOG"):
            return
        previous = transition.previous.upper()
        current = transition.current.upper()
        interesting = {
            ("OK", "DEGRADED"),
            ("DEGRADED", "OK"),
            ("DEGRADED", "AUTO_HOLD"),
            ("AUTO_HOLD", "OK"),
        }
        if (previous, current) not in interesting:
            return
        reason = transition.reason or "n/a"
        auto_hold = bool(transition.auto_hold)
        display_previous = "DEGRADED" if previous == "AUTO_HOLD" and current == "OK" else previous
        timestamp_iso = datetime.fromtimestamp(
            transition.timestamp, tz=timezone.utc
        ).isoformat()
        headline = (
            f"[WATCHDOG] {exchange}: {display_previous} -> {current}. "
            f"reason={reason} (auto_hold={str(auto_hold).lower()})"
        )
        extra = {
            "exchange": exchange,
            "previous": previous,
            "current": current,
            "reason": reason,
            "auto_hold": auto_hold,
            "timestamp": timestamp_iso,
        }
        try:
            notifier.emit_alert(
                "watchdog_status",
                headline,
                extra=extra,
            )
        except Exception:  # pragma: no cover - notification errors ignored
            LOGGER.debug("failed to emit watchdog alert", exc_info=True)

    def _handle_transitions(self, report: WatchdogCheckResult) -> None:
        for exchange, transition in report.transitions.items():
            self._maybe_emit_transition_alert(exchange, transition)

    async def _apply_policies(self, report: WatchdogCheckResult) -> None:
        overall_ok = self._watchdog.overall_ok()
        auto_hold = _env_flag("WATCHDOG_AUTO_HOLD")
        enabled = _env_flag("WATCHDOG_ENABLED")
        if not enabled or not auto_hold or overall_ok:
            self._last_overall_ok = overall_ok
            return
        failure = self._watchdog.most_recent_failure()
        if failure is None:
            self._last_overall_ok = overall_ok
            return
        runtime.evaluate_exchange_watchdog(context="watchdog_loop")
        exchange, payload = failure
        reason = str(payload.get("reason") or "degraded")
        transition = self._watchdog.mark_auto_hold(exchange, reason=reason)
        if transition is not None:
            self._maybe_emit_transition_alert(exchange, transition)
        self._last_overall_ok = overall_ok


_RUNNER = ExchangeWatchdogRunner(get_exchange_watchdog())


def get_runner() -> ExchangeWatchdogRunner:
    return _RUNNER


def setup_exchange_watchdog(app: FastAPI) -> None:
    app.state.exchange_watchdog = _RUNNER

    @app.on_event("startup")
    async def _start_runner() -> None:  # pragma: no cover - integration lifecycle
        await _RUNNER.start()

    @app.on_event("shutdown")
    async def _stop_runner() -> None:  # pragma: no cover - integration lifecycle
        await _RUNNER.stop()


__all__ = ["ExchangeWatchdogRunner", "setup_exchange_watchdog", "get_runner"]
