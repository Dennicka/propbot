from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

from fastapi import FastAPI

from ..audit_log import log_operator_action
from ..metrics import RECON_DIFFS_GAUGE, RECON_EXCEPTIONS_COUNTER
from ..recon import RECON_NOTIONAL_TOL_USDT, Reconciler
from . import runtime

LOGGER = logging.getLogger(__name__)

RECON_INTERVAL_SEC = 30.0
RECON_ENABLED = False
RECON_AUTO_HOLD = True


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
        reconciler: Reconciler | None = None,
        *,
        interval: float | None = None,
    ) -> None:
        self._reconciler = reconciler or Reconciler()
        self._interval = interval or _env_float("RECON_INTERVAL_SEC", RECON_INTERVAL_SEC)
        self._enabled = _env_flag("RECON_ENABLED", RECON_ENABLED)
        self._auto_hold = _env_flag("RECON_AUTO_HOLD", RECON_AUTO_HOLD)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def auto_hold_enabled(self) -> bool:
        return self._auto_hold

    @auto_hold_enabled.setter
    def auto_hold_enabled(self, enabled: bool) -> None:
        self._auto_hold = bool(enabled)

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
        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            diffs = self._reconciler.diff()
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.exception("reconciliation run failed")
            RECON_EXCEPTIONS_COUNTER.inc()
            runtime.update_reconciliation_status(
                diffs=[],
                desync_detected=True,
                last_checked=timestamp,
                metadata={"auto_hold": self._auto_hold, "error": str(exc)},
            )
            runtime.send_notifier_alert("recon_exception", f"[RECON] exception: {exc}")
            raise
        RECON_DIFFS_GAUGE.set(len(diffs))
        runtime.update_reconciliation_status(
            diffs=diffs,
            desync_detected=bool(diffs),
            last_checked=timestamp,
            metadata={"auto_hold": self._auto_hold},
        )
        if diffs:
            self._emit_alert(diffs)
            if self._auto_hold:
                severe = self._severe_diffs(diffs)
                if severe:
                    self._engage_hold(severe, timestamp)
        else:
            LOGGER.debug("reconciliation clean")
        return {"diffs": diffs, "auto_hold": self._auto_hold}

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

    def _emit_alert(self, diffs: Sequence[Mapping[str, object]]) -> None:
        worst = max(diffs, key=lambda entry: abs(float(entry.get("notional_usd", 0.0) or 0.0)))
        venue = str(worst.get("venue") or "unknown")
        symbol = str(worst.get("symbol") or "unknown")
        delta = float(worst.get("delta") or 0.0)
        notional = abs(float(worst.get("notional_usd", 0.0) or 0.0))
        headline = (
            f"[RECON] mismatch detected — {venue} {symbol} delta={delta:.6f}, "
            f"notional≈{notional:.2f}"
        )
        runtime.send_notifier_alert(
            "recon_diff",
            headline,
            extra={"diffs": list(diffs)},
        )

    def _severe_diffs(self, diffs: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
        severe: list[Mapping[str, object]] = []
        for diff in diffs:
            notional = abs(float(diff.get("notional_usd", 0.0) or 0.0))
            if notional > RECON_NOTIONAL_TOL_USDT:
                severe.append(diff)
        return severe

    def _engage_hold(
        self,
        diffs: Sequence[Mapping[str, object]],
        timestamp: str,
    ) -> None:
        reason = "auto_hold:reconciliation"
        engaged = runtime.engage_safety_hold(reason, source="reconciliation_runner")
        details = {
            "reason": reason,
            "diffs": list(diffs),
            "timestamp": timestamp,
        }
        if engaged:
            log_operator_action("system", "system", "AUTO_HOLD_RECON", details)
            runtime.send_notifier_alert("auto_hold_recon", "[RECON] AUTO-HOLD engaged", extra=details)
        else:
            LOGGER.info("AUTO-HOLD already active for reconciliation", extra={"reason": reason})


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
