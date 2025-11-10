from __future__ import annotations

import asyncio
from collections import Counter
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, Optional, Sequence

from .. import ledger, risk_governor
from ..broker.base import CancelAllResult
from ..journal import is_enabled as journal_enabled
from ..journal import order_journal
from ..metrics import set_auto_trade_state
from ..broker.router import ExecutionRouter
from . import arbitrage
from .dryrun import compute_metrics, select_cycle_symbol
from .runtime import (
    HoldActiveError,
    LoopState,
    engage_safety_hold,
    get_loop_state,
    get_safety_status,
    get_state,
    is_hold_active,
    register_cancel_attempt,
    send_notifier_alert,
    set_last_execution,
    set_last_plan,
    set_loop_config,
    set_open_orders,
    update_loop_summary,
    update_runaway_guard_snapshot,
)
from ..risk.runaway_guard import (
    RUNAWAY_GUARD_V2_SOURCE,
    RunawayGuardCooldownError,
    get_guard as get_runaway_guard,
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
            LOGGER.debug("loop worker cancelled")
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


def _normalise_cancel_all_result(
    result: object, *, orders: Sequence[Mapping[str, object]]
) -> CancelAllResult | None:
    if isinstance(result, CancelAllResult):
        return result
    if not isinstance(result, Mapping):
        return None
    cancelled_raw = result.get("cleared", result.get("cancelled"))
    cleared = 0
    total_orders = len(orders)
    if isinstance(cancelled_raw, str) and cancelled_raw.lower() == "all":
        cleared = total_orders
    elif isinstance(cancelled_raw, (int, float)):
        cleared = max(0, int(cancelled_raw))
    failed_raw = result.get("failed", 0)
    try:
        failed = max(0, int(float(failed_raw)))
    except (TypeError, ValueError):
        failed = 0
    ok_field = result.get("ok")
    ok = bool(ok_field) if ok_field is not None else failed == 0
    order_ids_raw = result.get("order_ids")
    order_ids: Sequence[int] = ()
    if isinstance(order_ids_raw, Iterable) and not isinstance(order_ids_raw, (str, bytes)):
        ids: list[int] = []
        for entry in order_ids_raw:
            try:
                ids.append(int(entry))
            except (TypeError, ValueError):
                continue
        order_ids = tuple(ids)
    if not order_ids and cleared >= total_orders and total_orders > 0:
        derived_ids: list[int] = []
        for order in orders:
            try:
                derived_ids.append(int(order.get("id", 0)))
            except (TypeError, ValueError):
                continue
        order_ids = tuple(id_ for id_ in derived_ids if id_)
    if cleared < total_orders and not order_ids:
        # Partial result without explicit IDs — defer to per-order cancellation.
        cleared = 0
    details = {key: value for key, value in result.items()}
    return CancelAllResult(
        ok=ok, cleared=cleared, failed=failed, order_ids=order_ids, details=details
    )


async def _call_cancel_all_orders_idempotent(
    broker,
    *,
    venue: str,
    orders: Sequence[Mapping[str, object]],
    correlation_id: str | None,
) -> CancelAllResult | None:
    method = getattr(broker, "cancel_all_orders_idempotent", None)
    if not callable(method):
        return None
    try:
        maybe = method(venue=venue, correlation_id=correlation_id, orders=orders)
    except TypeError:
        try:
            maybe = method(venue=venue, correlation_id=correlation_id)
        except TypeError:
            maybe = method(venue=venue)
    result = await maybe if inspect.isawaitable(maybe) else maybe
    return _normalise_cancel_all_result(result, orders=orders)


async def cancel_all_orders(
    venue: str | None = None,
    *,
    correlation_id: str | None = None,
) -> Dict[str, int]:
    journal_active = journal_enabled()
    if correlation_id and journal_active:
        existing = order_journal.get(correlation_id)
        if existing:
            payload = existing.get("payload") if isinstance(existing, dict) else None
            result_payload = payload.get("result") if isinstance(payload, dict) else None
            duplicate_result = (
                result_payload
                if isinstance(result_payload, dict)
                else {"cancelled": 0, "failed": 0}
            )
            order_journal.append(
                {
                    "type": "cancel_all.duplicate",
                    "payload": {
                        "correlation_id": correlation_id,
                        "result": duplicate_result,
                    },
                }
            )
            ledger.record_event(
                level="INFO",
                code="cancel_all_duplicate",
                payload={"correlation_id": correlation_id, "result": duplicate_result},
            )
            return duplicate_result
    venue_normalised = venue.lower() if venue else None
    orders = await asyncio.to_thread(ledger.fetch_open_orders)
    if venue_normalised:
        orders = [
            order for order in orders if str(order.get("venue", "")).lower() == venue_normalised
        ]
    set_open_orders(orders)
    if not orders:
        return {"cancelled": 0, "failed": 0}
    router = ExecutionRouter()
    if is_hold_active():
        safety = get_safety_status()
        raise HoldActiveError(safety.get("hold_reason") or "hold_active")
    guard = get_runaway_guard()
    guard_enabled = guard.feature_enabled()
    cancelled = 0
    failed = 0
    orders_snapshot = [
        {
            "id": int(order.get("id", 0)),
            "venue": order.get("venue"),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "qty": order.get("qty"),
            "status": order.get("status"),
        }
        for order in orders
    ]
    grouped_orders: Dict[str, Dict[str, Any]] = {}
    for order in orders:
        venue_value = str(order.get("venue") or "")
        venue_key = venue_value.lower()
        bucket = grouped_orders.setdefault(venue_key, {"display": venue_value, "orders": []})
        bucket["orders"].append(order)

    for venue_key, bucket in grouped_orders.items():
        venue_display = bucket["display"]
        venue_orders = list(bucket["orders"])
        if guard_enabled and guard.feature_enabled():
            symbol_counts = Counter()
            for entry in venue_orders:
                symbol_value = str(entry.get("symbol") or "")
                symbol_counts[symbol_value.upper()] += 1
            for symbol_key, planned in symbol_counts.items():
                if not guard.allow_cancel(venue_key, symbol_key, planned=planned):
                    block_details = guard.last_block() or {}
                    snapshot = guard.snapshot()
                    update_runaway_guard_snapshot(snapshot)
                    payload = {
                        "venue": venue_display,
                        "venue_key": venue_key,
                        "symbol": symbol_key,
                        "planned_cancels": planned,
                        "orders_in_venue": len(venue_orders),
                        "block": dict(block_details),
                        "max_cancels_per_min": snapshot.get("max_cancels_per_min"),
                        "cooldown_sec": snapshot.get("cooldown_sec"),
                    }
                    ledger.record_event(
                        level="WARNING",
                        code="cancel_all_blocked_runaway",
                        payload=payload,
                    )
                    reason = str(block_details.get("reason") or "")
                    if reason == "limit_exceeded":
                        reason_text = f"{RUNAWAY_GUARD_V2_SOURCE}:{venue_key}:{symbol_key}"
                        payload["reason"] = reason_text
                        send_notifier_alert(
                            "runaway_guard_v2_limit",
                            "Runaway guard engaged: cancel-all blocked",
                            payload,
                        )
                        engage_safety_hold(reason_text, source=RUNAWAY_GUARD_V2_SOURCE)
                        raise HoldActiveError(reason_text)
                    send_notifier_alert(
                        "runaway_guard_v2_cooldown",
                        "Runaway guard cooldown active",
                        payload,
                    )
                    raise RunawayGuardCooldownError(block_details or payload)
        broker = router.broker_for_venue(venue_display)
        idempotent_result = await _call_cancel_all_orders_idempotent(
            broker,
            venue=venue_display,
            orders=tuple(venue_orders),
            correlation_id=correlation_id,
        )
        pending_orders = list(venue_orders)
        if idempotent_result is not None:
            cleared_ids = {int(order_id) for order_id in idempotent_result.order_ids}
            if cleared_ids:
                await asyncio.gather(
                    *(
                        asyncio.to_thread(ledger.update_order_status, order_id, "cancelled")
                        for order_id in cleared_ids
                    )
                )
                pending_orders = [
                    order for order in pending_orders if int(order.get("id", 0)) not in cleared_ids
                ]
            cleared_count = max(0, int(idempotent_result.cleared))
            failed += max(0, int(idempotent_result.failed))
            cancelled += cleared_count
            if guard_enabled and guard.feature_enabled():
                ids_for_guard = cleared_ids
                if (
                    not ids_for_guard
                    and idempotent_result.ok
                    and cleared_count >= len(venue_orders)
                ):
                    ids_for_guard = {
                        int(order.get("id", 0)) for order in venue_orders if int(order.get("id", 0))
                    }
                if ids_for_guard:
                    for order in venue_orders:
                        order_id = int(order.get("id", 0))
                        if order_id in ids_for_guard:
                            guard.register_cancel(venue_key, str(order.get("symbol") or "").upper())
            if idempotent_result.ok and cleared_count >= len(venue_orders):
                if guard_enabled and guard.feature_enabled():
                    update_runaway_guard_snapshot(guard.snapshot())
                continue
        for order in pending_orders:
            venue_value = str(order.get("venue") or "")
            order_id = int(order.get("id", 0))
            try:
                register_cancel_attempt(
                    reason="runaway_cancels_per_min", source=f"cancel_all:{venue_value}"
                )
                await broker.cancel(venue=venue_value, order_id=order_id)
                cancelled += 1
                if guard_enabled and guard.feature_enabled():
                    guard.register_cancel(venue_key, str(order.get("symbol") or "").upper())
            except HoldActiveError:
                raise
            except Exception as exc:  # pragma: no cover - defensive logging
                failed += 1
                ledger.record_event(
                    level="ERROR",
                    code="cancel_failed",
                    payload={"venue": venue_value, "order_id": order_id, "error": str(exc)},
                )
        if guard_enabled and guard.feature_enabled():
            update_runaway_guard_snapshot(guard.snapshot())
    remaining = await asyncio.to_thread(ledger.fetch_open_orders)
    set_open_orders(remaining)
    result = {"cancelled": cancelled, "failed": failed}
    if journal_active:
        payload = {
            "correlation_id": correlation_id,
            "result": result,
            "orders": orders_snapshot,
            "remaining_open_orders": [int(order.get("id", 0)) for order in remaining],
            "status": "RESUMED" if correlation_id else "EXECUTED",
        }
        event = {
            "type": "cancel_all.executed",
            "payload": payload,
        }
        if correlation_id:
            event["uuid"] = correlation_id
        order_journal.append(event)
    return result


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
