from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from .. import ledger, risk_governor
from ..metrics import set_auto_trade_state
from ..broker.router import ExecutionRouter
from . import arbitrage
from .dryrun import compute_metrics, select_cycle_symbol
from .runtime import (
    HoldActiveError,
    LoopState,
    get_loop_state,
    get_safety_status,
    get_state,
    is_hold_active,
    register_cancel_attempt,
    set_last_execution,
    set_last_plan,
    set_loop_config,
    set_open_orders,
    update_loop_summary,
)

LOGGER = logging.getLogger(__name__)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LoopCycleSummary:
    status: str
    symbol: str
    spread_bps: float | None
    spread_usdt: float | None
    est_pnl_usdt: float | None
    est_pnl_bps: float | None
    reason: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "status": self.status,
            "symbol": self.symbol,
            "spread_bps": self.spread_bps,
            "spread_usdt": self.spread_usdt,
            "est_pnl_usdt": self.est_pnl_usdt,
            "est_pnl_bps": self.est_pnl_bps,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


@dataclass
class LoopCycleResult:
    ok: bool
    symbol: Optional[str] = None
    plan: Optional[Dict[str, Any]] = None
    execution: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    summary: Optional[LoopCycleSummary] = None


async def run_cycle(*, allow_safe_mode: bool = True) -> LoopCycleResult:
    state = get_state()
    loop_state = get_loop_state()
    symbol = select_cycle_symbol()
    notional = float(state.control.order_notional_usdt)
    slippage = int(state.control.max_slippage_bps)
    loop_state.running = True
    loop_state.status = "RUN"
    loop_state.last_cycle_ts = _ts()
    loop_state.pair = state.control.loop_pair or symbol
    loop_state.venues = list(state.control.loop_venues)
    loop_state.notional_usdt = notional
    plan = arbitrage.build_plan(symbol, notional, slippage)
    plan_payload = plan.as_dict()
    set_last_plan(plan_payload)
    loop_state.last_plan = plan_payload

    metrics = compute_metrics(plan)
    loop_state.last_spread_bps = metrics.spread_bps
    loop_state.last_spread_usdt = metrics.spread_usdt
    summary = LoopCycleSummary(
        status="pending",
        symbol=symbol,
        spread_bps=metrics.spread_bps,
        spread_usdt=metrics.spread_usdt,
        est_pnl_usdt=metrics.est_pnl_usdt,
        est_pnl_bps=metrics.est_pnl_bps,
    )

    hold_reason = await risk_governor.validate(context="loop")
    if hold_reason:
        loop_state.last_error = hold_reason
        loop_state.last_execution = None
        loop_state.status = "HOLD"
        loop_state.running = False
        summary.status = "blocked"
        summary.reason = hold_reason
        update_loop_summary(summary.as_dict())
        ledger.record_event(
            level="WARNING",
            code="risk_hold_engaged",
            payload={"context": "loop", "reason": hold_reason},
        )
        loop_state.cycles_completed += 1
        return LoopCycleResult(
            ok=False,
            symbol=symbol,
            plan=plan_payload,
            error=hold_reason,
            summary=summary,
        )

    if not plan.viable:
        reason = plan.reason or "plan not viable"
        loop_state.last_error = reason
        loop_state.last_execution = None
        summary.status = "rejected"
        summary.reason = reason
        update_loop_summary(summary.as_dict())
        LOGGER.info(
            "loop plan rejected",
            extra={
                "symbol": symbol,
                "reason": reason,
                "spread_bps": metrics.spread_bps,
                "pnl_usdt": metrics.est_pnl_usdt,
            },
        )
        ledger.record_event(
            level="WARNING",
            code="loop_plan_unviable",
            payload={
                "symbol": symbol,
                "reason": reason,
                "spread_bps": metrics.spread_bps,
                "pnl_usdt": metrics.est_pnl_usdt,
            },
        )
        loop_state.cycles_completed += 1
        return LoopCycleResult(
            ok=False,
            symbol=symbol,
            plan=plan_payload,
            error=reason,
            summary=summary,
        )

    try:
        report = await arbitrage.execute_plan_async(plan, allow_safe_mode=allow_safe_mode)
    except Exception as exc:  # pragma: no cover - defensive logging
        error = str(exc)
        loop_state.last_error = error
        LOGGER.exception("loop execution failed")
        loop_state.last_execution = None
        summary.status = "error"
        summary.reason = error
        update_loop_summary(summary.as_dict())
        ledger.record_event(
            level="ERROR",
            code="loop_execution_failed",
            payload={
                "symbol": symbol,
                "error": error,
                "spread_bps": metrics.spread_bps,
                "pnl_usdt": metrics.est_pnl_usdt,
            },
        )
        loop_state.cycles_completed += 1
        return LoopCycleResult(
            ok=False,
            symbol=symbol,
            plan=plan_payload,
            error=error,
            summary=summary,
        )

    execution_payload = report.as_dict()
    set_last_execution(execution_payload)
    loop_state.last_execution = execution_payload
    loop_state.last_error = None
    summary.status = "executed"
    summary.reason = None
    update_loop_summary(summary.as_dict())
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
    loop_state.cycles_completed += 1
    return LoopCycleResult(
        ok=True,
        symbol=symbol,
        plan=plan_payload,
        execution=execution_payload,
        summary=summary,
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
            state = get_state()
            state.control.auto_loop = True
            set_auto_trade_state(True)
            loop_state.pair = state.control.loop_pair or loop_state.pair
            loop_state.venues = list(state.control.loop_venues)
            loop_state.notional_usdt = state.control.order_notional_usdt
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
            state = get_state()
            state.control.auto_loop = False
            set_auto_trade_state(False)
            return loop_state

    async def reset(self) -> LoopState:
        async with self._lock:
            await self._cancel_locked()
            state = get_state()
            state.loop = LoopState()
            state.control.auto_loop = False
            set_auto_trade_state(False)
            return state.loop

    async def stop_after_cycle(self) -> LoopState:
        async with self._lock:
            loop_state = get_loop_state()
            if loop_state.status == "RUN":
                loop_state.status = "STOPPING"
            state = get_state()
            state.control.auto_loop = False
            set_auto_trade_state(False)
            return loop_state

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
                if loop_state.status == "STOPPING":
                    loop_state.status = "HOLD"
                    state.control.auto_loop = False
                    set_auto_trade_state(False)
                    break
                if loop_state.status != "RUN":
                    state.control.auto_loop = False
                    set_auto_trade_state(False)
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
            loop_state = get_loop_state()
            loop_state.running = False
            if loop_state.status == "RUN":
                loop_state.status = "HOLD"
            state = get_state()
            state.control.auto_loop = loop_state.status == "RUN"
            set_auto_trade_state(state.control.auto_loop)


_CONTROLLER = LoopController()


def get_controller() -> LoopController:
    return _CONTROLLER


async def resume_loop() -> LoopState:
    return await _CONTROLLER.resume()


async def hold_loop() -> LoopState:
    return await _CONTROLLER.hold()


async def stop_loop() -> LoopState:
    return await _CONTROLLER.stop_after_cycle()


async def cancel_all_orders(venue: str | None = None) -> Dict[str, int]:
    venue_normalised = venue.lower() if venue else None
    orders = await asyncio.to_thread(ledger.fetch_open_orders)
    if venue_normalised:
        orders = [order for order in orders if str(order.get("venue", "")).lower() == venue_normalised]
    set_open_orders(orders)
    if not orders:
        return {"cancelled": 0, "failed": 0}
    router = ExecutionRouter()
    if is_hold_active():
        safety = get_safety_status()
        raise HoldActiveError(safety.get("hold_reason") or "hold_active")
    cancelled = 0
    failed = 0
    for order in orders:
        venue = str(order.get("venue") or "")
        order_id = int(order.get("id", 0))
        broker = router.broker_for_venue(venue)
        try:
            register_cancel_attempt(reason="runaway_cancels_per_min", source=f"cancel_all:{venue}")
            await broker.cancel(venue=venue, order_id=order_id)
            cancelled += 1
        except HoldActiveError:
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            failed += 1
            ledger.record_event(
                level="ERROR",
                code="cancel_failed",
                payload={"venue": venue, "order_id": order_id, "error": str(exc)},
            )
    remaining = await asyncio.to_thread(ledger.fetch_open_orders)
    set_open_orders(remaining)
    return {"cancelled": cancelled, "failed": failed}


async def reset_loop() -> LoopState:
    return await _CONTROLLER.reset()


async def loop_forever(
    cycles: int | None = None,
    allow_safe_mode: Optional[bool] = None,
    on_cycle: Callable[[LoopCycleResult], Awaitable[None] | None] | None = None,
) -> None:
    count = 0
    loop_state = get_loop_state()
    loop_state.status = "RUN"
    loop_state.running = True
    state = get_state()
    state.control.auto_loop = True
    set_auto_trade_state(True)
    try:
        while True:
            state = get_state()
            allow = allow_safe_mode
            if allow is None:
                allow = state.control.safe_mode or state.control.dry_run
            result = await run_cycle(allow_safe_mode=bool(allow))
            if on_cycle:
                maybe = on_cycle(result)
                if inspect.isawaitable(maybe):
                    await maybe
            summary_payload = result.summary.as_dict() if result.summary else {}
            LOGGER.info(
                "cycle summary",
                extra={
                    "status": summary_payload.get("status", "unknown"),
                    "symbol": summary_payload.get("symbol", result.symbol),
                    "spread_bps": summary_payload.get("spread_bps"),
                    "pnl_usdt": summary_payload.get("est_pnl_usdt"),
                    "reason": summary_payload.get("reason"),
                },
            )
            count += 1
            if cycles is not None and count >= cycles:
                break
            interval = max(1, int(state.control.poll_interval_sec))
            await asyncio.sleep(interval)
    finally:
        state = get_state()
        state.control.auto_loop = False
        loop_state.running = False
        if cycles is not None and cycles > 0:
            loop_state.status = "HOLD"
        set_auto_trade_state(False)


def loop_snapshot() -> Dict[str, Any]:
    return get_loop_state().as_dict()

