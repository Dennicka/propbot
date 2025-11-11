"""Asynchronous runner that periodically plans and executes partial hedges."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, MutableMapping

from fastapi import FastAPI

from ..broker.router import ExecutionRouter
from ..utils.symbols import normalise_symbol
from ..util.venues import VENUE_ALIASES
from .. import ledger
from ..hedge.partial import PartialHedgePlanner
from . import portfolio
from .runtime import (
    engage_safety_hold,
    get_state,
    is_hold_active,
    register_order_attempt,
)


LOGGER = logging.getLogger(__name__)

PlanOrders = list[dict[str, Any]]
ResidualProvider = Callable[[], Awaitable[list[dict[str, Any]]]]
OrderExecutor = Callable[[PlanOrders], Awaitable[list[dict[str, Any]]]]


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


def _env_interval() -> float:
    return max(_env_float("HEDGE_INTERVAL_SEC", 15.0), 1.0)


def _env_min_notional() -> float:
    return max(_env_float("HEDGE_MIN_NOTIONAL_USDT", 50.0), 0.0)


def _env_max_notional_per_order() -> float:
    return max(_env_float("HEDGE_MAX_NOTIONAL_USDT_PER_ORDER", 5_000.0), 1.0)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _RunnerState:
    enabled: bool = False
    dry_run: bool = True
    last_plan: dict[str, Any] | None = None
    last_execution: dict[str, Any] | None = None
    last_error: str | None = None
    failure_streak: int = 0
    auto_hold_triggered: bool = False
    plan_counter: int = 0
    last_snapshot_ts: str | None = None
    totals: dict[str, Any] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "last_plan": self.last_plan,
            "last_execution": self.last_execution,
            "last_error": self.last_error,
            "failure_streak": self.failure_streak,
            "auto_hold_triggered": self.auto_hold_triggered,
            "plan_counter": self.plan_counter,
            "last_snapshot_ts": self.last_snapshot_ts,
            "totals": dict(self.totals),
        }


_STATE = _RunnerState(
    enabled=_env_flag("HEDGE_ENABLED", False),
    dry_run=_env_flag("HEDGE_DRY_RUN", True),
)
_STATE_LOCK = asyncio.Lock()


def _resolve_taker_fee(venue: str) -> float:
    state = get_state()
    venue_key = VENUE_ALIASES.get(venue.lower(), venue.lower())
    if "binance" in venue_key:
        return float(getattr(state.control, "taker_fee_bps_binance", 3))
    if "okx" in venue_key:
        return float(getattr(state.control, "taker_fee_bps_okx", 3))
    return float(getattr(state.control, "default_taker_fee_bps", 5))


def _normalise_venue(value: str) -> str:
    return VENUE_ALIASES.get(value.lower(), value.lower())


@dataclass
class HedgeTotals:
    orders: int = 0
    notional_usdt: float = 0.0
    symbols: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "orders": self.orders,
            "notional_usdt": self.notional_usdt,
            "symbols": {symbol: dict(payload) for symbol, payload in self.symbols.items()},
        }


class PartialHedgeRunner:
    def __init__(
        self,
        *,
        planner: PartialHedgePlanner | None = None,
        residuals_provider: ResidualProvider | None = None,
        order_executor: OrderExecutor | None = None,
        interval: float | None = None,
        enabled: bool | None = None,
        dry_run: bool | None = None,
    ) -> None:
        self._interval = interval or _env_interval()
        self._planner = planner or PartialHedgePlanner(
            min_notional_usdt=_env_min_notional(),
            max_notional_usdt_per_order=_env_max_notional_per_order(),
        )
        self._residuals_provider = residuals_provider or self._collect_residuals
        self._order_executor = order_executor or self._default_execute_orders
        if enabled is not None:
            _STATE.enabled = bool(enabled)
        if dry_run is not None:
            _STATE.dry_run = bool(dry_run)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._router = ExecutionRouter()
        self._runner_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return _STATE.enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        _STATE.enabled = bool(value)

    @property
    def dry_run(self) -> bool:
        return _STATE.dry_run

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        _STATE.dry_run = bool(value)

    async def start(self) -> None:
        if not self.enabled:
            LOGGER.info("partial hedge runner disabled")
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:  # pragma: no cover - lifecycle cleanup
            LOGGER.debug("partial hedge runner cancelled during stop")
        finally:
            self._task = None

    async def _run_loop(self) -> None:
        LOGGER.info("partial hedge runner started interval=%ss", self._interval)
        while not self._stop.is_set():
            try:
                await self.run_cycle()
            except asyncio.CancelledError:  # pragma: no cover - lifecycle cleanup
                break
            except Exception as exc:  # pragma: no cover - defensive
                LOGGER.exception("partial hedge cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue
        LOGGER.info("partial hedge runner stopped")

    async def run_cycle(self) -> dict[str, Any]:
        async with self._runner_lock:
            if not self.enabled:
                return await self._record_plan_snapshot(
                    {
                        "status": "disabled",
                        "orders": [],
                        "totals": {},
                        "generated_ts": _iso_now(),
                    }
                )
            residuals = await self._residuals_provider()
            orders = self._planner.plan(residuals)
            plan_details = self._planner.last_plan_details
            snapshot = {
                "status": "planned",
                "orders": orders,
                "plan": plan_details,
                "generated_ts": plan_details.get("generated_ts", _iso_now()),
            }
            snapshot = await self._record_plan_snapshot(snapshot)
            if orders and not self.dry_run:
                execution = await self._attempt_execution(orders)
                snapshot["execution"] = execution
            return snapshot

    async def plan_once(self, *, execute: bool = False) -> dict[str, Any]:
        async with self._runner_lock:
            residuals = await self._residuals_provider()
            orders = self._planner.plan(residuals)
            plan_details = self._planner.last_plan_details
            snapshot = {
                "status": "planned",
                "orders": orders,
                "plan": plan_details,
                "generated_ts": plan_details.get("generated_ts", _iso_now()),
            }
            snapshot = await self._record_plan_snapshot(snapshot, manual=True)
            if execute and orders:
                execution = await self._attempt_execution(orders, manual=True)
                snapshot["execution"] = execution
            return snapshot

    async def _record_plan_snapshot(
        self, snapshot: dict[str, Any], *, manual: bool = False
    ) -> dict[str, Any]:
        plan_details = snapshot.get("plan") or self._planner.last_plan_details
        totals = HedgeTotals()
        plan_orders = plan_details.get("orders") if isinstance(plan_details, Mapping) else None
        if isinstance(plan_orders, Iterable):
            for entry in plan_orders:
                if not isinstance(entry, Mapping):
                    continue
                totals.orders += 1
                try:
                    notional = float(entry.get("notional_usdt", 0.0))
                except (TypeError, ValueError):
                    notional = 0.0
                totals.notional_usdt += max(notional, 0.0)
        totals.symbols = {
            symbol: dict(payload)
            for symbol, payload in plan_details.get("symbols", {}).items()
            if isinstance(payload, Mapping)
        }
        async with _STATE_LOCK:
            _STATE.last_plan = plan_details
            _STATE.last_snapshot_ts = snapshot.get("generated_ts") or _iso_now()
            _STATE.plan_counter += 1
            _STATE.totals = totals.as_dict()
            if manual:
                _STATE.last_error = None
        return snapshot

    async def _attempt_execution(
        self, orders: PlanOrders, *, manual: bool = False
    ) -> dict[str, Any]:
        state = get_state()
        if is_hold_active():
            message = "hold_active"
            await self._set_error(message)
            return {"status": "blocked", "reason": message}
        if state.control.safe_mode:
            message = "safe_mode"
            await self._set_error(message)
            return {"status": "blocked", "reason": message}
        if state.control.two_man_rule and len(state.control.approvals) < 2:
            message = "two_man_rule_missing"
            await self._set_error(message)
            return {"status": "blocked", "reason": message}
        try:
            execution = await self._order_executor(orders)
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.exception("partial hedge execution failed: %s", exc)
            await self._register_failure(str(exc))
            return {"status": "error", "error": str(exc)}
        await self._register_success()
        payload = {"status": "executed", "orders": execution, "manual": manual}
        async with _STATE_LOCK:
            _STATE.last_execution = payload
        return payload

    async def _set_error(self, reason: str) -> None:
        async with _STATE_LOCK:
            _STATE.last_error = reason

    async def _register_success(self) -> None:
        async with _STATE_LOCK:
            _STATE.failure_streak = 0
            _STATE.last_error = None

    async def _register_failure(self, reason: str) -> None:
        async with _STATE_LOCK:
            _STATE.failure_streak += 1
            _STATE.last_error = reason
            streak = _STATE.failure_streak
        lowered = reason.lower()
        if streak > 3 and ("insufficient" in lowered or "price out of bounds" in lowered):
            already_triggered = False
            async with _STATE_LOCK:
                already_triggered = _STATE.auto_hold_triggered
                _STATE.auto_hold_triggered = True
            if not already_triggered:
                engage_safety_hold("partial_hedge:auto_hold", source="partial_hedge")

    async def _collect_residuals(self) -> list[dict[str, Any]]:
        snapshot = await portfolio.snapshot()
        residuals: list[dict[str, Any]] = []
        positions_payload: list[dict[str, Any]] = []
        for position in snapshot.positions:
            qty = float(position.qty)
            if abs(qty) <= 1e-9:
                continue
            symbol = normalise_symbol(position.symbol)
            venue = _normalise_venue(position.venue)
            residuals.append(
                {
                    "venue": venue,
                    "symbol": symbol,
                    "side": "LONG" if qty > 0 else "SHORT",
                    "qty": abs(qty),
                    "strategy": "portfolio_snapshot",
                    "notional_usdt": abs(position.notional),
                    "funding_apr": None,
                    "taker_fee_bps": _resolve_taker_fee(venue),
                    "maker_fee_bps": 0.0,
                }
            )
            positions_payload.append(
                {
                    "venue": venue,
                    "symbol": symbol,
                    "base_qty": qty,
                    "avg_price": position.entry_px,
                }
            )
        balances_payload: list[dict[str, Any]] = []
        for balance in snapshot.balances:
            balances_payload.append(
                {
                    "venue": _normalise_venue(balance.venue),
                    "asset": balance.asset,
                    "qty": balance.free,
                }
            )
        self._planner.update_market_snapshot(positions=positions_payload, balances=balances_payload)
        return residuals

    async def _default_execute_orders(self, orders: PlanOrders) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index, order in enumerate(orders):
            venue = str(order.get("venue") or "")
            symbol = str(order.get("symbol") or "")
            side = str(order.get("side") or "").lower()
            qty = float(order.get("qty", 0.0) or 0.0)
            if qty <= 0:
                continue
            register_order_attempt(
                reason="partial_hedge",
                source=f"partial_hedge:{venue}:{symbol}:{side}",
            )
            broker = self._router.broker_for_venue(venue)
            try:
                result = await broker.create_order(
                    venue=venue,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    type="MARKET",
                    post_only=False,
                    reduce_only=False,
                    idemp_key=f"partial-hedge-{_iso_now()}-{index}",
                )
            except Exception as exc:
                await self._register_failure(str(exc))
                raise
            results.append(result)
            await asyncio.to_thread(
                ledger.record_event,
                level="INFO",
                code="partial_hedge_order",
                payload={
                    "venue": venue,
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "idx": index,
                    "result": result,
                },
            )
        return results

    def status(self) -> dict[str, Any]:
        return _STATE.snapshot()


_RUNNER = PartialHedgeRunner()


def get_runner() -> PartialHedgeRunner:
    return _RUNNER


async def _startup_runner() -> None:  # pragma: no cover - FastAPI lifecycle
    runner = get_runner()
    if runner.enabled:
        await runner.start()


async def _shutdown_runner() -> None:  # pragma: no cover - FastAPI lifecycle
    await get_runner().stop()


def setup_partial_hedge_runner(app: FastAPI) -> None:
    app.state.partial_hedge_runner = get_runner()

    @app.on_event("startup")
    async def _on_startup() -> None:  # pragma: no cover - lifecycle glue
        await _startup_runner()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:  # pragma: no cover - lifecycle glue
        await _shutdown_runner()


def get_partial_hedge_status() -> dict[str, Any]:
    return get_runner().status()


async def execute_now(confirm: bool) -> dict[str, Any]:
    if not confirm:
        raise ValueError("confirmation required")
    return await get_runner().plan_once(execute=True)


async def refresh_plan() -> dict[str, Any]:
    return await get_runner().plan_once(execute=False)


def reset_state_for_tests() -> None:
    """Reset cached snapshot for deterministic unit tests."""

    _STATE.enabled = _env_flag("HEDGE_ENABLED", False)
    _STATE.dry_run = _env_flag("HEDGE_DRY_RUN", True)
    _STATE.last_plan = None
    _STATE.last_execution = None
    _STATE.last_error = None
    _STATE.failure_streak = 0
    _STATE.auto_hold_triggered = False
    _STATE.plan_counter = 0
    _STATE.last_snapshot_ts = None
    _STATE.totals = {}


__all__ = [
    "PartialHedgeRunner",
    "setup_partial_hedge_runner",
    "get_runner",
    "get_partial_hedge_status",
    "execute_now",
    "refresh_plan",
    "reset_state_for_tests",
]
