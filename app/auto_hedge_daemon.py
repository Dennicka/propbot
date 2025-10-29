"""Background auto-hedge daemon wiring opportunity scanner to execution."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping

from fastapi import FastAPI

from positions import create_position

from services.cross_exchange_arb import execute_hedged_trade
from services.opportunity_scanner import get_scanner
from services.risk_manager import can_open_new_position

from .services import risk_guard
from .services.hedge_log import append_entry
from .services.pnl_history import record_snapshot
from .services.runtime import (
    get_state,
    is_dry_run_mode,
    is_hold_active,
    set_last_opportunity_state,
    update_auto_hedge_state,
)


logger = logging.getLogger(__name__)

INITIATOR = "YOUR_NAME_OR_TOKEN"
_FAIL_WINDOW = 60.0

STRATEGY_NAME = "cross_exchange_arb"


def _emit_ops_alert(kind: str, text: str, extra: Mapping[str, object] | None = None) -> None:
    try:
        from .opsbot.notifier import emit_alert
    except Exception:
        return
    try:
        emit_alert(kind=kind, text=text, extra=extra or None)
    except Exception:
        pass


def _env_flag(name: str, default: bool = False) -> bool:
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


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _maybe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class AutoHedgeDaemon:
    def __init__(
        self,
        *,
        interval: float | None = None,
        max_failures: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._scanner = get_scanner()
        self._interval = max(interval or _env_float("AUTO_HEDGE_SCAN_SECS", 2.0), 0.5)
        self._max_failures = max_failures if max_failures is not None else _env_int("MAX_AUTO_FAILS_PER_MIN", 3)
        self._enabled_override = enabled
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._failure_events: list[float] = []

    def _is_enabled(self) -> bool:
        if self._enabled_override is not None:
            return self._enabled_override
        return _env_flag("AUTO_HEDGE_ENABLED", False)

    def _refresh_fail_window(self, now: float | None = None) -> None:
        if not self._failure_events:
            return
        now = now or time.time()
        cutoff = now - _FAIL_WINDOW
        self._failure_events = [ts for ts in self._failure_events if ts >= cutoff]

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
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception("auto hedge cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    def _check_system_state(self) -> tuple[bool, str | None]:
        if is_hold_active():
            return False, "hold_active"
        state = get_state()
        if state.control.mode != "RUN":
            return False, f"mode={state.control.mode.lower()}"
        if state.control.safe_mode:
            return False, "safe_mode"
        if getattr(state.control, "dry_run", False):
            return False, "dry_run"
        resume = state.safety.resume_request
        if resume and getattr(resume, "approved_ts", None) is None:
            return False, "two_man_pending"
        guard = state.guards.get("runaway_breaker")
        if guard and guard.status == "HOLD":
            return False, "runaway_guard_hold"
        counters = state.safety.counters
        limits = state.safety.limits
        if limits.max_orders_per_min > 0 and counters.orders_placed_last_min >= limits.max_orders_per_min:
            return False, "runaway_orders_limit"
        if limits.max_cancels_per_min > 0 and counters.cancels_last_min >= limits.max_cancels_per_min:
            return False, "runaway_cancels_limit"
        if state.risk.breaches:
            return False, "risk_breach_active"
        return True, None

    def _build_log_entry(
        self,
        *,
        candidate: Mapping[str, Any] | None,
        result: str,
        timestamp: str,
        trade_result: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        candidate_data: Dict[str, Any] = dict(candidate) if isinstance(candidate, Mapping) else {}
        trade_data: Dict[str, Any] = dict(trade_result) if isinstance(trade_result, Mapping) else {}
        return {
            "timestamp": timestamp,
            "symbol": str(candidate_data.get("symbol") or trade_data.get("symbol") or ""),
            "long_venue": str(
                trade_data.get("cheap_exchange")
                or candidate_data.get("long_venue")
                or candidate_data.get("cheap_exchange")
                or ""
            ),
            "short_venue": str(
                trade_data.get("expensive_exchange")
                or candidate_data.get("short_venue")
                or candidate_data.get("expensive_exchange")
                or ""
            ),
            "notional_usdt": _coerce_float(candidate_data.get("notional_suggestion")),
            "leverage": _coerce_float(candidate_data.get("leverage_suggestion")),
            "spread_bps": _coerce_float(
                trade_data.get("spread_bps", candidate_data.get("spread_bps"))
            ),
            "result": result,
            "status": str(trade_data.get("status") or result),
            "simulated": bool(trade_data.get("simulated")),
            "dry_run_mode": bool(trade_data.get("dry_run_mode")),
            "initiator": INITIATOR,
        }

    def _register_failure(
        self,
        *,
        reason: str,
        candidate: Mapping[str, Any] | None,
        trade_result: Mapping[str, Any] | None = None,
        timestamp: str | None = None,
    ) -> None:
        now = time.time()
        self._failure_events.append(now)
        self._refresh_fail_window(now)
        failures = len(self._failure_events)
        ts = timestamp or _ts()
        update_auto_hedge_state(
            last_execution_result=f"error: {reason}",
            last_execution_ts=ts,
            consecutive_failures=failures,
        )
        append_entry(
            self._build_log_entry(
                candidate=candidate, result=f"rejected: {reason}", timestamp=ts, trade_result=trade_result
            )
        )
        if self._max_failures > 0 and failures > self._max_failures:
            risk_guard.force_hold(
                risk_guard.REASON_AUTO_HEDGE_FAILURES,
                extra={
                    "reason": reason,
                    "consecutive_failures": failures,
                },
            )
        logger.warning("auto hedge rejected: %s", reason)
        alert_payload: Dict[str, object] = {"failures": failures, "reason": reason}
        if isinstance(candidate, Mapping):
            alert_payload["symbol"] = candidate.get("symbol")
        if isinstance(trade_result, Mapping):
            alert_payload["result"] = trade_result.get("result")
        _emit_ops_alert("auto_hedge_failure", f"Auto hedge failure: {reason}", alert_payload)

    async def run_cycle(self) -> None:
        enabled = self._is_enabled()
        update_auto_hedge_state(enabled=enabled)
        if not enabled:
            self._failure_events.clear()
            update_auto_hedge_state(last_execution_result="disabled", consecutive_failures=0)
            return

        self._max_failures = _env_int("MAX_AUTO_FAILS_PER_MIN", self._max_failures)
        self._refresh_fail_window()
        ok, reason = self._check_system_state()
        if not ok:
            update_auto_hedge_state(
                last_execution_result=f"rejected: {reason}",
                consecutive_failures=len(self._failure_events),
            )
            logger.info("auto hedge skipped: %s", reason)
            return

        try:
            scan_payload = await self._scanner.scan_once()
        except Exception as exc:
            logger.exception("auto hedge scan failed: %s", exc)
            self._register_failure(reason="scanner_error", candidate=None)
            return

        candidate = None
        status = ""
        if isinstance(scan_payload, Mapping):
            candidate = scan_payload.get("candidate")
            status = str(scan_payload.get("status") or "")

        ts_checked = _ts()
        update_auto_hedge_state(last_checked_ts=ts_checked)

        if not isinstance(candidate, Mapping):
            update_auto_hedge_state(
                last_execution_result=f"rejected: {status or 'no_candidate'}",
                consecutive_failures=len(self._failure_events),
            )
            return

        candidate = dict(candidate)
        if status and status != "allowed":
            update_auto_hedge_state(
                last_execution_result=f"rejected: {status}",
                consecutive_failures=len(self._failure_events),
            )
            return

        spread_value = _coerce_float(candidate.get("spread"))
        min_spread = _coerce_float(candidate.get("min_spread", candidate.get("spread")))
        if spread_value <= 0 or spread_value < min_spread:
            self._register_failure(reason="spread_below_threshold", candidate=candidate, timestamp=_ts())
            set_last_opportunity_state(candidate, "blocked_by_risk")
            return

        notional = _coerce_float(candidate.get("notional_suggestion"))
        leverage = _coerce_float(candidate.get("leverage_suggestion"))
        allowed, reason = can_open_new_position(notional, leverage)
        if not allowed:
            self._register_failure(reason=f"risk:{reason}", candidate=candidate, timestamp=_ts())
            set_last_opportunity_state(candidate, "blocked_by_risk")
            return

        symbol = str(candidate.get("symbol") or "")

        try:
            trade_result = execute_hedged_trade(symbol, notional, leverage, min_spread)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("auto hedge execution raised: %s", exc)
            self._register_failure(reason="execution_error", candidate=candidate)
            return

        if not trade_result.get("success"):
            failure_reason = str(trade_result.get("reason") or "execution_failed")
            self._register_failure(reason=failure_reason, candidate=candidate, trade_result=trade_result)
            set_last_opportunity_state(candidate, "blocked_by_risk")
            return

        simulated = bool(trade_result.get("simulated"))
        long_order = trade_result.get("long_order") or {}
        short_order = trade_result.get("short_order") or {}
        long_price = _maybe_float(
            long_order.get("price")
            or long_order.get("avg_price")
            or trade_result.get("details", {}).get("cheap_mark")
        )
        short_price = _maybe_float(
            short_order.get("price")
            or short_order.get("avg_price")
            or trade_result.get("details", {}).get("expensive_mark")
        )
        position = create_position(
            symbol=symbol,
            long_venue=str(trade_result.get("cheap_exchange") or candidate.get("long_venue") or ""),
            short_venue=str(trade_result.get("expensive_exchange") or candidate.get("short_venue") or ""),
            notional_usdt=notional,
            entry_spread_bps=_coerce_float(trade_result.get("spread_bps", candidate.get("spread_bps"))),
            leverage=leverage,
            entry_long_price=long_price,
            entry_short_price=short_price,
            status="simulated" if simulated else "open",
            simulated=simulated,
            legs=trade_result.get("legs"),
            strategy=STRATEGY_NAME,
        )
        trade_result["position"] = position
        ts = _ts()
        update_auto_hedge_state(
            last_execution_result="ok",
            last_execution_ts=ts,
            last_success_ts=ts,
            consecutive_failures=0,
        )
        self._failure_events.clear()
        append_entry(
            self._build_log_entry(
                candidate=candidate, result="accepted", timestamp=ts, trade_result=trade_result
            )
        )
        set_last_opportunity_state(None, "blocked_by_risk")
        logger.info(
            "auto hedge %s %s/%s notional=%s spread_bps=%s",
            "simulated" if simulated else "executed",
            trade_result.get("cheap_exchange"),
            trade_result.get("expensive_exchange"),
            notional,
            trade_result.get("spread_bps"),
        )
        alert_payload = {
            "symbol": symbol,
            "notional_usdt": notional,
            "spread_bps": trade_result.get("spread_bps"),
            "simulated": simulated,
            "dry_run_mode": bool(trade_result.get("dry_run_mode")),
        }
        if leverage is not None:
            alert_payload["leverage"] = leverage
        alert_text = (
            f"Auto hedge simulated for {symbol} (DRY_RUN_MODE)"
            if simulated or is_dry_run_mode()
            else f"Auto hedge executed for {symbol}"
        )
        _emit_ops_alert("auto_hedge_executed", alert_text, alert_payload)
        try:
            await record_snapshot(reason="auto_hedge_cycle")
        except Exception:  # pragma: no cover - snapshot failures should not break cycle
            logger.debug("failed to record pnl history snapshot", exc_info=True)


_daemon = AutoHedgeDaemon()


def setup_auto_hedge_daemon(app: FastAPI) -> None:
    app.state.auto_hedge_daemon = _daemon

    @app.on_event("startup")
    async def _start_daemon() -> None:  # pragma: no cover - integration hook
        await _daemon.start()

    @app.on_event("shutdown")
    async def _stop_daemon() -> None:  # pragma: no cover - integration hook
        await _daemon.stop()


__all__ = ["AutoHedgeDaemon", "setup_auto_hedge_daemon"]
