"""Daemon responsible for topping up partially hedged positions."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from collections.abc import Iterable, Mapping
from typing import Callable, Dict

from positions import list_open_positions, update_position

from app.ledger import record_order
from app.services.runtime import (
    HoldActiveError,
    get_state,
    is_hold_active,
    register_order_attempt,
)
from app.strategy_risk import get_strategy_risk_manager
from services.cross_exchange_arb import _client_for, _normalise_order


LOGGER = logging.getLogger(__name__)

EPSILON = 1e-6


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0


def _leg_for(position: Mapping[str, object], side: str) -> Mapping[str, object] | None:
    legs = position.get("legs")
    if not isinstance(legs, Iterable):
        return None
    side_norm = side.lower()
    for leg in legs:
        if isinstance(leg, Mapping) and str(leg.get("side") or "").lower() == side_norm:
            return leg
    return None


def _leg_base_size(leg: Mapping[str, object] | None) -> float:
    if not isinstance(leg, Mapping):
        return 0.0
    for key in ("base_size", "filled_qty", "qty"):
        value = leg.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


class PartialHedgeRebalancer:
    def __init__(
        self,
        *,
        interval: float | None = None,
        retry_delay: float | None = None,
        batch_notional: float | None = None,
        max_retry: int | None = None,
        client_factory: Callable[[str], object] | None = None,
    ) -> None:
        self._interval = max(interval or _env_float("REBALANCER_INTERVAL_SEC", 5.0), 0.5)
        self._retry_delay = retry_delay or _env_float("REBALANCER_RETRY_DELAY_SEC", 30.0)
        self._batch_notional = batch_notional or _env_float("REBALANCER_BATCH_NOTIONAL_USD", 1_000.0)
        self._max_retry = max_retry if max_retry is not None else _env_int("REBALANCER_MAX_RETRY", 5)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._client_factory = client_factory or _client_for

    def _feature_enabled(self) -> bool:
        return _env_flag("FEATURE_REBALANCER", False)

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
                LOGGER.exception("rebalancer cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    def _check_state(self) -> tuple[bool, str | None]:
        if not self._feature_enabled():
            return False, "feature_disabled"
        if is_hold_active():
            return False, "hold_active"
        state = get_state()
        if state.control.mode != "RUN":
            return False, f"mode={state.control.mode.lower()}"
        if state.control.safe_mode:
            return False, "safe_mode"
        if getattr(state.control, "dry_run", False):
            return False, "dry_run"
        return True, None

    async def run_cycle(self) -> None:
        ok, reason = self._check_state()
        if not ok:
            LOGGER.debug("partial rebalancer idle", extra={"reason": reason})
            return
        positions = list_open_positions()
        for position in positions:
            status = str(position.get("status") or "").lower()
            if status != "partial":
                continue
            try:
                self._rebalance_position(position)
            except Exception:  # pragma: no cover - defensive per-position
                LOGGER.exception(
                    "failed to rebalance position", extra={"position_id": position.get("id")}
                )

    def _rebalance_position(self, position: Mapping[str, object]) -> None:
        position_id = str(position.get("id") or "")
        if not position_id:
            return
        meta = dict(position.get("rebalancer") or {})
        now_ts = time.time()
        notional = float(position.get("notional_usdt") or 0.0)
        base_size = float(position.get("base_size") or 0.0)
        entry_long = float(position.get("entry_long_price") or 0.0)
        entry_short = float(position.get("entry_short_price") or 0.0)
        target_base = base_size
        if target_base <= 0.0 and entry_long > 0:
            target_base = notional / entry_long
        if target_base <= 0.0 and entry_short > 0:
            target_base = notional / entry_short
        if target_base <= 0.0:
            return

        long_leg = _leg_for(position, "long")
        short_leg = _leg_for(position, "short")
        long_base = _leg_base_size(long_leg)
        short_base = _leg_base_size(short_leg)

        long_ratio = long_base / target_base if target_base else 0.0
        short_ratio = short_base / target_base if target_base else 0.0
        filled_ratio = min(long_ratio, short_ratio)

        meta_changed = False

        def _update_meta(**kwargs) -> None:
            nonlocal meta_changed
            for key, value in kwargs.items():
                if meta.get(key) != value:
                    meta[key] = value
                    meta_changed = True

        _update_meta(filled_ratio=round(filled_ratio, 6))

        if filled_ratio >= 1.0 - EPSILON:
            updates: Dict[str, object] = {
                "status": "open",
                "rebalancer": meta,
            }
            legs_override: list[Mapping[str, object]] = []
            if long_leg:
                legs_override.append({"side": "long", "status": "open"})
            if short_leg:
                legs_override.append({"side": "short", "status": "open"})
            update_position(position_id, updates=updates, legs=legs_override)
            return

        attempts = int(meta.get("attempts", 0) or 0)
        if attempts >= self._max_retry:
            _update_meta(status="exhausted")
            if meta_changed:
                update_position(position_id, updates={"rebalancer": meta})
            return

        last_attempt_ts = _parse_ts(meta.get("last_attempt_ts"))
        if last_attempt_ts and now_ts - last_attempt_ts < self._retry_delay:
            _update_meta(status="waiting")
            if meta_changed:
                update_position(position_id, updates={"rebalancer": meta})
            return

        strategy = str(position.get("strategy") or "").strip() or "cross_exchange_arb"
        risk_manager = get_strategy_risk_manager()
        if not risk_manager.is_enabled(strategy):
            _update_meta(status="disabled", last_error="strategy_disabled")
            if meta_changed:
                update_position(position_id, updates={"rebalancer": meta})
            return
        if risk_manager.is_frozen(strategy):
            _update_meta(status="frozen", last_error="strategy_frozen")
            if meta_changed:
                update_position(position_id, updates={"rebalancer": meta})
            return

        sides: list[str] = []
        if long_ratio < 1.0 - EPSILON:
            sides.append("long")
        if short_ratio < 1.0 - EPSILON:
            sides.append("short")
        leverage = float(position.get("leverage") or long_leg.get("leverage") if long_leg else 1.0)
        timestamp = _iso_now()

        for side in sides:
            remaining_base = target_base - (long_base if side == "long" else short_base)
            if remaining_base <= EPSILON:
                continue
            venue = str(position.get(f"{side}_venue") or (long_leg if side == "long" else short_leg or {}).get("venue") or "")
            if not venue:
                continue
            symbol = str((long_leg if side == "long" else short_leg or {}).get("symbol") or position.get("symbol") or "")
            try:
                client = self._client_factory(venue)
            except Exception as exc:
                attempts += 1
                _update_meta(
                    attempts=attempts,
                    last_attempt_ts=timestamp,
                    last_error=str(exc),
                    status="error",
                )
                update_position(position_id, updates={"rebalancer": meta})
                return
            try:
                mark = client.get_mark_price(symbol)
                mark_price = float(mark.get("mark_price") or mark.get("price") or 0.0)
            except Exception:
                mark_price = float((long_leg if side == "long" else short_leg or {}).get("entry_price") or entry_long or entry_short or 0.0)
            if mark_price <= 0:
                mark_price = 1.0
            batch_notional = float(min(self._batch_notional, remaining_base * mark_price))
            if batch_notional <= 0:
                batch_notional = remaining_base * mark_price
            try:
                register_order_attempt(reason="runaway_orders_per_min", source=f"partial_rebalance_{side}")
                order = client.place_order(symbol, side, batch_notional, leverage)
            except HoldActiveError as exc:
                _update_meta(status="hold", last_error=str(exc.reason or "hold_active"))
                if meta_changed:
                    update_position(position_id, updates={"rebalancer": meta})
                return
            except Exception as exc:
                attempts += 1
                _update_meta(
                    attempts=attempts,
                    last_attempt_ts=timestamp,
                    last_error=str(exc),
                    status="error",
                )
                update_position(position_id, updates={"rebalancer": meta})
                return

            normalised = _normalise_order(
                order=order,
                exchange=venue,
                symbol=symbol,
                side=side,
                notional_usdt=batch_notional,
                leverage=leverage,
                fallback_price=mark_price,
            )
            filled_qty = float(normalised.get("filled_qty") or 0.0)
            if filled_qty <= 0:
                attempts += 1
                _update_meta(
                    attempts=attempts,
                    last_attempt_ts=timestamp,
                    last_error="no_fill",
                    status="error",
                )
                update_position(position_id, updates={"rebalancer": meta})
                return

            attempts += 1
            if side == "long":
                long_base += filled_qty
                long_ratio = long_base / target_base
            else:
                short_base += filled_qty
                short_ratio = short_base / target_base
            filled_ratio = min(long_ratio, short_ratio)
            _update_meta(
                attempts=attempts,
                last_attempt_ts=timestamp,
                last_error=None,
                status="rebalancing",
                last_side=side,
                filled_ratio=round(filled_ratio, 6),
            )

            leg_status = "open" if filled_ratio >= 1.0 - EPSILON else "partial"
            legs_override = [
                {
                    "side": side,
                    "venue": venue,
                    "symbol": symbol,
                    "entry_price": normalised.get("avg_price"),
                    "base_size": long_base if side == "long" else short_base,
                    "filled_qty": long_base if side == "long" else short_base,
                    "status": leg_status,
                    "timestamp": timestamp,
                    "raw": normalised.get("raw"),
                }
            ]
            status_update = "open" if filled_ratio >= 1.0 - EPSILON else "partial"
            update_position(
                position_id,
                updates={"rebalancer": meta, "status": status_update},
                legs=legs_override,
            )
            try:
                record_order(
                    venue=venue,
                    symbol=symbol,
                    side=side,
                    qty=filled_qty,
                    price=float(normalised.get("avg_price") or mark_price),
                    status=str(normalised.get("status") or "filled"),
                    client_ts=timestamp,
                    exchange_ts=None,
                    idemp_key=f"rebalance:{position_id}:{side}:{attempts}:{int(now_ts)}",
                )
            except Exception:  # pragma: no cover - ledger failures shouldn't abort
                LOGGER.exception(
                    "failed to record rebalance order", extra={"position_id": position_id}
                )

            if filled_ratio >= 1.0 - EPSILON:
                _update_meta(status="settled")
                update_position(position_id, updates={"rebalancer": meta})
                return

        if meta_changed:
            update_position(position_id, updates={"rebalancer": meta})


__all__ = ["PartialHedgeRebalancer"]

