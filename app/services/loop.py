from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .. import ledger
from . import arbitrage
from .dryrun import compute_metrics, select_cycle_symbol
from .runtime import (
    LoopState,
    get_loop_state,
    get_state,
    set_last_execution,
    set_last_plan,
)

LOGGER = logging.getLogger(__name__)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LoopCycleResult:
    ok: bool
    symbol: Optional[str] = None
    plan: Optional[Dict[str, Any]] = None
    execution: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


async def run_cycle(*, allow_safe_mode: bool = True) -> LoopCycleResult:
    state = get_state()
    loop_state = get_loop_state()
    symbol = select_cycle_symbol()
    notional = float(state.control.order_notional_usdt)
    slippage = int(state.control.max_slippage_bps)
    loop_state.running = True
    loop_state.status = "RUN"
    loop_state.last_cycle_ts = _ts()
    plan = arbitrage.build_plan(symbol, notional, slippage)
    plan_payload = plan.as_dict()
    set_last_plan(plan_payload)
    loop_state.last_plan = plan_payload

    if not plan.viable:
        reason = plan.reason or "plan not viable"
        loop_state.last_error = reason
        loop_state.last_execution = None
        LOGGER.info("loop plan rejected", extra={"symbol": symbol, "reason": reason})
        ledger.record_event(
            level="WARNING",
            code="loop_plan_unviable",
            payload={"symbol": symbol, "reason": reason},
        )
        return LoopCycleResult(ok=False, symbol=symbol, plan=plan_payload, error=reason)

    try:
        report = await arbitrage.execute_plan_async(plan, allow_safe_mode=allow_safe_mode)
    except Exception as exc:  # pragma: no cover - defensive logging
        error = str(exc)
        loop_state.last_error = error
        LOGGER.exception("loop execution failed")
        ledger.record_event(
            level="ERROR",
            code="loop_execution_failed",
            payload={"symbol": symbol, "error": error},
        )
        return LoopCycleResult(ok=False, symbol=symbol, plan=plan_payload, error=error)

    execution_payload = report.as_dict()
    set_last_execution(execution_payload)
    loop_state.last_execution = execution_payload
    loop_state.last_error = None
    loop_state.cycles_completed += 1

    metrics = compute_metrics(plan)
    loop_state.last_spread_bps = metrics.spread_bps
    loop_state.last_spread_usdt = metrics.spread_usdt

    ledger.record_event(
        level="INFO",
        code="loop_cycle",
        payload={
            "symbol": symbol,
            "ok": True,
            "viable": plan.viable,
            "spread_bps": metrics.spread_bps,
            "pnl_usdt": metrics.est_pnl_usdt,
        },
    )
    LOGGER.info(
        "loop cycle completed",
        extra={
            "symbol": symbol,
            "viable": plan.viable,
            "spread_bps": metrics.spread_bps,
            "pnl_usdt": metrics.est_pnl_usdt,
        },
    )
    return LoopCycleResult(
        ok=True,
        symbol=symbol,
        plan=plan_payload,
        execution=execution_payload,
    )


class LoopController:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def resume(self) -> LoopState:
        async with self._lock:
            loop_state = get_loop_state()
            loop_state.status = "RUN"
            loop_state.running = True
            if self._task and not self._task.done():
                return loop_state
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._worker())
            return loop_state

    async def hold(self) -> LoopState:
        async with self._lock:
            await self._cancel_locked()
            loop_state = get_loop_state()
            loop_state.status = "HOLD"
            loop_state.running = False
            return loop_state

    async def reset(self) -> LoopState:
        async with self._lock:
            await self._cancel_locked()
            state = get_state()
            state.loop = LoopState()
            return state.loop

    async def _cancel_locked(self) -> None:
        if self._task is None:
            return
        task, self._task = self._task, None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover - expected path
            pass

    async def _worker(self) -> None:
        try:
            while True:
                state = get_state()
                allow_safe = state.control.safe_mode or state.control.dry_run
                await run_cycle(allow_safe_mode=allow_safe)
                loop_state = get_loop_state()
                if loop_state.status != "RUN":
                    break
                interval = max(1, int(state.control.poll_interval_sec))
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover - defensive logging
            error = str(exc)
            loop_state = get_loop_state()
            loop_state.last_error = error
            loop_state.running = False
            ledger.record_event(level="ERROR", code="loop_worker_failed", payload={"error": error})
            LOGGER.exception("loop worker crashed")
        finally:
            get_loop_state().running = False


_CONTROLLER = LoopController()


def get_controller() -> LoopController:
    return _CONTROLLER


async def resume_loop() -> LoopState:
    return await _CONTROLLER.resume()


async def hold_loop() -> LoopState:
    return await _CONTROLLER.hold()


async def reset_loop() -> LoopState:
    return await _CONTROLLER.reset()


async def loop_forever(cycles: int | None = None, allow_safe_mode: Optional[bool] = None) -> None:
    count = 0
    loop_state = get_loop_state()
    loop_state.status = "RUN"
    loop_state.running = True
    try:
        while True:
            state = get_state()
            allow = allow_safe_mode
            if allow is None:
                allow = state.control.safe_mode or state.control.dry_run
            await run_cycle(allow_safe_mode=bool(allow))
            count += 1
            if cycles is not None and count >= cycles:
                break
            interval = max(1, int(state.control.poll_interval_sec))
            await asyncio.sleep(interval)
    finally:
        loop_state.running = False
        if cycles is not None and cycles > 0:
            loop_state.status = "HOLD"


def loop_snapshot() -> Dict[str, Any]:
    return get_loop_state().as_dict()

