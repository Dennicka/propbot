"""Idempotent order router with persistent intent ledger."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Tuple

from ..audit_log import log_operator_action
from ..execution.order_state import OrderState, OrderStatus, apply_exchange_update
from .. import ledger
from ..golden.logger import get_golden_logger
from ..metrics import (
    IDEMPOTENCY_HIT_TOTAL,
    ORDER_INTENT_TOTAL,
    ORDER_SUBMIT_LATENCY,
    PRETRADE_BLOCKS_TOTAL,
    observe_replace_chain,
    record_open_intents,
)
from ..rules.pretrade import PretradeValidationError, get_pretrade_validator
from ..persistence import order_store
from ..runtime import locks
from ..services import runtime
from ..utils.identifiers import generate_request_id
from .adapter import generate_client_order_id
from ..risk.exposure_caps import (
    check_open_allowed,
    collect_snapshot,
    resolve_caps,
    snapshot_entry,
)
from ..risk.freeze import get_freeze_registry


LOGGER = logging.getLogger(__name__)


_CRITICAL_CAUSE = "ACCOUNT_HEALTH_CRITICAL"
_REDUCE_ONLY_BLOCK = "blocked: reduce-only due to ACCOUNT_HEALTH::CRITICAL"
_FROZEN_BLOCK = "blocked: FROZEN_BY_RISK"


def _coerce_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _maybe_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


_FILLED_STATUSES = {"FILLED", "DONE", "COMPLETED", "CLOSED"}
_PARTIAL_STATUSES = {"PARTIALLY_FILLED", "PARTIAL", "PARTIAL_FILL"}
_CANCELED_STATUSES = {"CANCELED", "CANCELLED", "CANCELLED_BY_USER", "USER_CANCELED"}
_REJECTED_STATUSES = {"REJECTED", "FAILED", "ERROR"}
_EXPIRED_STATUSES = {"EXPIRED", "DEAD", "ABANDONED"}


def _extract_filled_qty(payload: Mapping[str, object]) -> float:
    for key in (
        "filled_qty",
        "filled",
        "executed_qty",
        "executedQty",
        "cum_exec_qty",
        "cumExecQty",
    ):
        if key in payload:
            return _coerce_float(payload.get(key))
    return 0.0


def _extract_remaining_qty(payload: Mapping[str, object]) -> float | None:
    for key in ("remaining_qty", "leaves_qty", "leavesQty", "remaining"):
        if key in payload:
            return _coerce_float(payload.get(key))
    return None


def _extract_avg_price(payload: Mapping[str, object]) -> float | None:
    for key in ("avg_fill_price", "avg_price", "price_avg", "fill_price"):
        if key in payload:
            value = _coerce_float(payload.get(key))
            if value > 0:
                return value
    return None


_INTENT_TO_ORDER_STATUS = {
    order_store.OrderIntentState.NEW: OrderStatus.NEW,
    order_store.OrderIntentState.PENDING: OrderStatus.PENDING,
    order_store.OrderIntentState.SENT: OrderStatus.PENDING,
    order_store.OrderIntentState.ACKED: OrderStatus.ACK,
    order_store.OrderIntentState.PARTIAL: OrderStatus.PARTIAL,
    order_store.OrderIntentState.FILLED: OrderStatus.FILLED,
    order_store.OrderIntentState.CANCELED: OrderStatus.CANCELED,
    order_store.OrderIntentState.REJECTED: OrderStatus.REJECTED,
    order_store.OrderIntentState.EXPIRED: OrderStatus.EXPIRED,
    order_store.OrderIntentState.REPLACED: OrderStatus.CANCELED,
}

_ORDER_STATUS_TO_INTENT = {
    OrderStatus.NEW: order_store.OrderIntentState.NEW,
    OrderStatus.PENDING: order_store.OrderIntentState.PENDING,
    OrderStatus.ACK: order_store.OrderIntentState.ACKED,
    OrderStatus.PARTIAL: order_store.OrderIntentState.PARTIAL,
    OrderStatus.FILLED: order_store.OrderIntentState.FILLED,
    OrderStatus.CANCELED: order_store.OrderIntentState.CANCELED,
    OrderStatus.REJECTED: order_store.OrderIntentState.REJECTED,
    OrderStatus.EXPIRED: order_store.OrderIntentState.EXPIRED,
}


def _resolve_remote_state(
    intent: order_store.OrderIntent,
    payload: Mapping[str, object],
) -> tuple[
    order_store.OrderIntentState,
    float,
    float | None,
    float | None,
    str | None,
]:
    current_state = OrderState(
        status=_INTENT_TO_ORDER_STATUS.get(intent.state, OrderStatus.NEW),
        qty=float(intent.qty or 0.0),
        cum_filled=float(intent.filled_qty or 0.0),
        avg_price=float(intent.avg_fill_price) if intent.avg_fill_price else None,
    )
    status_keys = ("status", "state", "order_status")
    if not any(key in payload for key in status_keys) and not current_state.is_terminal():
        enriched = dict(payload)
        enriched["status"] = OrderStatus.ACK.value
        payload_for_update: Mapping[str, object] = enriched
    else:
        payload_for_update = payload
    new_state = apply_exchange_update(current_state, payload_for_update)
    target_state = _ORDER_STATUS_TO_INTENT.get(
        new_state.status, order_store.OrderIntentState.ACKED
    )
    filled_qty = new_state.cum_filled
    remaining_qty: float | None
    if new_state.qty > 0:
        remaining_qty = max(new_state.qty - new_state.cum_filled, 0.0)
    else:
        remaining_qty = None
    avg_price = new_state.avg_price
    return target_state, filled_qty, remaining_qty, avg_price, new_state.last_event


def _resolve_gate(ctx: object) -> object | None:
    runtime_scope = getattr(ctx, "runtime", ctx)
    gate = getattr(runtime_scope, "pre_trade_gate", None)
    if gate is not None:
        return gate
    getter = getattr(runtime_scope, "get_pre_trade_gate", None)
    if callable(getter):
        try:
            return getter()
        except TypeError as exc:
            LOGGER.debug(
                "pre-trade gate getter failed",
                extra={"error": str(exc), "scope": type(runtime_scope).__name__},
            )
    return None


def enforce_reduce_only(
    ctx: object,
    symbol: str,
    side: str,
    qty: float | int | str | None,
    current_position: object,
) -> Tuple[bool, bool, str | None]:
    """Allow reduce-only orders when the pre-trade gate is health-critical."""

    gate = _resolve_gate(ctx)
    if gate is None:
        return True, False, None
    is_critical = False
    check = getattr(gate, "is_throttled_by", None)
    if callable(check):
        try:
            is_critical = bool(check(_CRITICAL_CAUSE))
        except Exception:  # pragma: no cover - defensive
            is_critical = False
    else:
        is_critical = bool(getattr(gate, "is_throttled", False)) and (
            (getattr(gate, "reason", "") or "").strip() == _CRITICAL_CAUSE
        )
    if not is_critical:
        return True, False, None

    qty_value = _coerce_float(qty)
    if qty_value <= 0:
        native_supported = bool(getattr(ctx, "native_reduce_only", False))
        return True, native_supported, None

    position_value: float
    if isinstance(current_position, Mapping):
        for key in ("qty", "base_qty", "position", "value"):
            if key in current_position:
                position_value = _coerce_float(current_position.get(key))
                break
        else:
            position_value = 0.0
    elif hasattr(current_position, "qty"):
        position_value = _coerce_float(getattr(current_position, "qty"))
    else:
        position_value = _coerce_float(current_position)

    if abs(position_value) <= 1e-9:
        return False, False, _REDUCE_ONLY_BLOCK

    side_value = str(side or "").lower()
    if side_value not in {"buy", "sell"}:
        return False, False, _REDUCE_ONLY_BLOCK

    if side_value == "buy":
        projected = position_value + qty_value
    else:
        projected = position_value - qty_value

    native_supported = bool(getattr(ctx, "native_reduce_only", False))

    # Allow reductions to zero without flipping to the opposite side.
    if position_value > 0:
        if projected < -1e-9:
            return False, False, _REDUCE_ONLY_BLOCK
        if abs(projected) > abs(position_value) + 1e-9:
            return False, False, _REDUCE_ONLY_BLOCK
    elif position_value < 0:
        if projected > 1e-9:
            return False, False, _REDUCE_ONLY_BLOCK
        if abs(projected) > abs(position_value) + 1e-9:
            return False, False, _REDUCE_ONLY_BLOCK

    return True, native_supported, None


def _is_reduce_only_order(side: str, qty: float | int | str | None, current_position: float) -> bool:
    qty_value = _coerce_float(qty)
    if qty_value <= 0:
        return False
    side_value = str(side or "").lower()
    if side_value not in {"buy", "sell"}:
        return False
    if abs(current_position) <= 1e-9:
        return False
    if current_position > 0 and side_value == "sell":
        return qty_value <= current_position + 1e-9
    if current_position < 0 and side_value == "buy":
        return qty_value <= abs(current_position) + 1e-9
    return False


class OrderRouterError(RuntimeError):
    """Raised when the order router encounters an unrecoverable error."""


class PretradeGateThrottled(RuntimeError):
    """Raised when the pre-trade gate blocks an order submission."""

    def __init__(self, reason: str, *, details: Mapping[str, Any] | None = None) -> None:
        message = reason or "PRETRADE_BLOCKED"
        super().__init__(message)
        self.reason = message
        self.details = dict(details or {})


@dataclass(slots=True)
class OrderRef:
    intent_id: str
    request_id: str
    broker_order_id: str | None
    state: order_store.OrderIntentState


class OrderRouter:
    def __init__(self, broker) -> None:
        self._broker = broker

    def _supports_native_reduce_only(self, venue: str) -> bool:
        support = getattr(self._broker, "supports_reduce_only", None)
        if isinstance(support, Mapping):
            key = str(venue or "").lower()
            return bool(support.get(key) or support.get(str(venue or "")))
        if callable(support):
            try:
                return bool(support(venue=venue))
            except TypeError:
                try:
                    return bool(support(venue))
                except TypeError:
                    return bool(support())
        if isinstance(support, (set, frozenset, list, tuple)):
            candidates = {str(item).lower() for item in support}
            return str(venue or "").lower() in candidates
        if isinstance(support, bool):
            return support
        attr = getattr(self._broker, "native_reduce_only_venues", None)
        if isinstance(attr, (set, frozenset, list, tuple)):
            candidates = {str(item).lower() for item in attr}
            return str(venue or "").lower() in candidates
        if isinstance(attr, Mapping):
            key = str(venue or "").lower()
            return bool(attr.get(key) or attr.get(str(venue or "")))
        return False

    def _current_position(self, venue: str, symbol: str) -> float:
        try:
            rows = ledger.fetch_positions()
        except Exception:  # pragma: no cover - defensive read
            return 0.0
        symbol_key = str(symbol or "").upper()
        venue_key = str(venue or "").lower()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            row_symbol = str(row.get("symbol") or "").upper()
            row_venue = str(row.get("venue") or "").lower()
            if row_symbol != symbol_key or row_venue != venue_key:
                continue
            value = row.get("base_qty")
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    async def submit_order(
        self,
        *,
        account: str,
        venue: str,
        symbol: str,
        side: str,
        order_type: str,
        qty: float,
        price: float | None = None,
        tif: str | None = None,
        strategy: str | None = None,
        intent_id: str | None = None,
        request_id: str | None = None,
    ) -> OrderRef:
        validator = get_pretrade_validator()
        order_payload: Dict[str, object] = {
            "venue": venue,
            "symbol": symbol,
            "qty": qty,
            "price": price,
            "side": side,
            "type": order_type,
            "tif": tif,
            "strategy": strategy,
        }
        reduce_only_flag = False
        registry = get_freeze_registry()
        if registry.is_frozen(strategy=strategy, venue=venue, symbol=symbol):
            current_position = self._current_position(venue, symbol)
            if _is_reduce_only_order(side, qty, current_position):
                reduce_only_flag = self._supports_native_reduce_only(venue)
            else:
                reason_text = _FROZEN_BLOCK
                PRETRADE_BLOCKS_TOTAL.labels(reason=reason_text).inc()
                runtime.record_pretrade_block(symbol, reason_text, qty=qty, price=price)
                log_operator_action(
                    "system",
                    "system",
                    "PRETRADE_BLOCKED",
                    details={
                        "reason": reason_text,
                        "venue": venue,
                        "symbol": symbol,
                        "qty": qty,
                        "price": price,
                        "strategy": strategy,
                    },
                )
                raise PretradeGateThrottled(reason_text, details=order_payload)
        gate = runtime.get_pre_trade_gate()
        allowed, gate_reason = gate.check_allowed(order_payload)
        if not allowed:
            ctx = SimpleNamespace(
                runtime=runtime,
                native_reduce_only=self._supports_native_reduce_only(venue),
            )
            current_position = self._current_position(venue, symbol)
            reduce_allowed, native_supported, reduce_reason = enforce_reduce_only(
                ctx, symbol, side, qty, current_position
            )
            if reduce_allowed:
                allowed = True
                gate_reason = None
                reduce_only_flag = reduce_only_flag or native_supported
            else:
                reason_text = (
                    (reduce_reason or gate_reason or "PRETRADE_BLOCKED").strip()
                    or "PRETRADE_BLOCKED"
                )
                PRETRADE_BLOCKS_TOTAL.labels(reason=reason_text).inc()
                runtime.record_pretrade_block(symbol, reason_text, qty=qty, price=price)
                log_operator_action(
                    "system",
                    "system",
                    "PRETRADE_BLOCKED",
                    details={
                        "reason": reason_text,
                        "venue": venue,
                        "symbol": symbol,
                        "qty": qty,
                        "price": price,
                    },
                )
                raise PretradeGateThrottled(reason_text, details=order_payload)
        ok, reason, fixed = validator.validate(order_payload)
        if not ok:
            raise PretradeValidationError(reason or "PRETRADE_INVALID", details=order_payload)
        if fixed:
            order_payload.update(fixed)
        qty = float(order_payload.get("qty", qty))
        price_value = order_payload.get("price", price)
        price = float(price_value) if price_value is not None else None

        snapshot = collect_snapshot()
        entry, _, _ = snapshot_entry(snapshot, symbol=symbol, venue=venue)
        current_base_qty = _coerce_float(entry.get("base_qty"))
        side_value = str(order_payload.get("side") or side or "").lower()
        if side_value == "buy":
            delta_qty = qty
        elif side_value == "sell":
            delta_qty = -qty
        else:
            delta_qty = 0.0
        new_base_qty = current_base_qty + delta_qty
        target_side: str | None
        if new_base_qty > 1e-9:
            target_side = "LONG"
        elif new_base_qty < -1e-9:
            target_side = "SHORT"
        else:
            target_side = None
        projected_price = price if price is not None and price > 0 else 0.0
        if projected_price <= 0:
            notional_value = _coerce_float(order_payload.get("notional"))
            if abs(qty) > 1e-9 and notional_value > 0:
                projected_price = notional_value / abs(qty)
            else:
                projected_price = _coerce_float(entry.get("avg_price"))
        price_reference = projected_price
        if price_reference <= 0 and abs(qty) > 1e-9:
            price_reference = _coerce_float(order_payload.get("notional")) / abs(qty)
        if price_reference <= 0:
            price_reference = _coerce_float(entry.get("avg_price"))
        price_reference = max(price_reference, 0.0)
        current_long_abs = _coerce_float(entry.get("LONG"))
        current_short_abs = _coerce_float(entry.get("SHORT"))
        notional_value = _coerce_float(order_payload.get("notional"))
        new_abs_position = 0.0
        current_side_abs = 0.0
        if target_side == "LONG":
            current_side_abs = current_long_abs
            if current_base_qty >= -1e-9:
                increment = 0.0
                if delta_qty > 0:
                    increment = (
                        notional_value
                        if notional_value > 0
                        else delta_qty * price_reference
                    )
                new_abs_position = current_long_abs + max(increment, 0.0)
            else:
                new_abs_position = abs(new_base_qty) * price_reference
        elif target_side == "SHORT":
            current_side_abs = current_short_abs
            if current_base_qty <= 1e-9:
                increment = 0.0
                if delta_qty < 0:
                    increment = (
                        notional_value
                        if notional_value > 0
                        else (-delta_qty) * price_reference
                    )
                new_abs_position = current_short_abs + max(increment, 0.0)
            else:
                new_abs_position = abs(new_base_qty) * price_reference
        increasing = bool(target_side) and (
            new_abs_position > current_side_abs + 1e-6
        )
        if increasing and target_side is not None:
            state = runtime.get_state()
            caps_ctx: Dict[str, object] = {
                "config": state.config.data,
                "snapshot": snapshot,
            }
            allowed, cap_reason = check_open_allowed(
                caps_ctx,
                symbol,
                target_side,
                venue,
                new_abs_position,
            )
            if not allowed:
                reason_text = cap_reason or "EXPOSURE_CAPS::UNKNOWN"
                PRETRADE_BLOCKS_TOTAL.labels(reason=reason_text).inc()
                runtime.record_pretrade_block(symbol, reason_text, qty=qty, price=price)
                projection = caps_ctx.get("projection")
                caps_payload = resolve_caps(state.config.data, symbol, target_side, venue)
                ledger.record_event(
                    level="WARNING",
                    code="exposure_caps_block",
                    payload={
                        "symbol": symbol,
                        "venue": venue,
                        "side": target_side,
                        "reason": reason_text,
                        "qty": qty,
                        "price": price,
                        "projected_global": projection.get("global", {}).get("projected")
                        if isinstance(projection, Mapping)
                        else None,
                        "projected_side": projection.get("side", {}).get("projected")
                        if isinstance(projection, Mapping)
                        else None,
                        "projected_venue": projection.get("venue_total", {}).get("projected")
                        if isinstance(projection, Mapping)
                        else None,
                        "cap_global": caps_payload.get("global_max_abs"),
                        "cap_side": caps_payload.get("side_max_abs"),
                        "cap_venue": caps_payload.get("venue_max_abs"),
                    },
                )
                LOGGER.warning(
                    "pretrade_blocked exposure caps",
                    extra={
                        "symbol": symbol,
                        "venue": venue,
                        "side": target_side,
                        "reason": reason_text,
                    },
                )
                log_operator_action(
                    "system",
                    "system",
                    "PRETRADE_BLOCKED",
                    details={
                        "reason": reason_text,
                        "venue": venue,
                        "symbol": symbol,
                        "qty": qty,
                        "price": price,
                    },
                )
                raise PretradeValidationError(reason_text, details=order_payload)

        intent_key = intent_id or request_id or generate_request_id()
        async with locks.intent_lock(intent_key):
            prev_state: order_store.OrderIntentState | None = None
            prev_request_id: str | None = None
            prev_broker_order_id: str | None = None
            created_ts_hint: float | None = None
            with order_store.session_scope() as session:
                existing = order_store.load_intent(session, intent_key)
                if existing is not None:
                    prev_state = existing.state
                    prev_request_id = existing.request_id
                    prev_broker_order_id = existing.broker_order_id
                    if existing.created_ts is not None:
                        created_ts_hint = existing.created_ts.timestamp()
                if request_id:
                    client_request_id = request_id
                elif prev_request_id:
                    client_request_id = prev_request_id
                else:
                    client_request_id = generate_client_order_id(
                        strategy=strategy,
                        venue=venue,
                        symbol=symbol,
                        side=side,
                        timestamp=created_ts_hint,
                        nonce=intent_key,
                    )
                intent = order_store.ensure_order_intent(
                    session,
                    intent_id=intent_key,
                    request_id=client_request_id,
                    account=account,
                    venue=venue,
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    qty=float(qty),
                    price=float(price) if price is not None else None,
                    tif=tif,
                    strategy=strategy,
                )
                current_state = intent.state
                if (
                    prev_request_id == client_request_id
                    and current_state in order_store.COMPLETED_FOR_IDEMPOTENCY
                ):
                    IDEMPOTENCY_HIT_TOTAL.labels(operation="submit").inc()
                    LOGGER.info(
                        "duplicate order suppressed",
                        extra={
                            "intent_id": intent.intent_id,
                            "broker_order_id": intent.broker_order_id,
                            "state": intent.state.value,
                        },
                    )
                    return OrderRef(
                        intent_id=intent.intent_id,
                        request_id=intent.request_id,
                        broker_order_id=intent.broker_order_id,
                        state=intent.state,
                    )
                if (
                    prev_request_id == client_request_id
                    and current_state
                    in (
                        order_store.OrderIntentState.PENDING,
                        order_store.OrderIntentState.SENT,
                    )
                ):
                    IDEMPOTENCY_HIT_TOTAL.labels(operation="submit").inc()
                    return OrderRef(
                        intent_id=intent.intent_id,
                        request_id=intent.request_id,
                        broker_order_id=intent.broker_order_id,
                        state=intent.state,
                    )
                try:
                    order_store.update_intent_state(
                        session,
                        intent,
                        state=order_store.OrderIntentState.PENDING,
                    )
                except order_store.OrderStateTransitionError as exc:
                    LOGGER.exception(
                        "order intent transition failed",
                        extra={
                            "intent_id": intent.intent_id,
                            "from_state": prev_state.value if prev_state else None,
                            "to_state": order_store.OrderIntentState.PENDING.value,
                        },
                    )
                    raise OrderRouterError("order state transition failed") from exc

            start = time.perf_counter()
            try:
                payload = await self._broker.create_order(
                    venue=venue,
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    price=price,
                    type=order_type,
                    tif=tif,
                    strategy=strategy,
                    idemp_key=client_request_id,
                    reduce_only=reduce_only_flag,
                )
            except Exception as exc:  # pragma: no cover - broker errors propagate
                LOGGER.exception(
                    "order submit failed",
                    extra={"intent_id": intent_key, "venue": venue, "error": str(exc)},
                )
                with order_store.session_scope() as session:
                    intent = order_store.load_intent(session, intent_key)
                    if intent:
                        try:
                            order_store.update_intent_state(
                                session,
                                intent,
                                state=order_store.OrderIntentState.REJECTED,
                            )
                        except order_store.OrderStateTransitionError:
                            LOGGER.exception(
                                "order intent reject transition failed",
                                extra={"intent_id": intent_key},
                            )
                raise OrderRouterError("order submit failed") from exc
            finally:
                duration_ms = (time.perf_counter() - start) * 1000.0
                ORDER_SUBMIT_LATENCY.observe(duration_ms)

            broker_order_id = _extract_order_id(payload)
            with order_store.session_scope() as session:
                intent = order_store.load_intent(session, intent_key)
                if not intent:
                    raise OrderRouterError("intent missing after submit")
                try:
                    order_store.update_intent_state(
                        session,
                        intent,
                        state=order_store.OrderIntentState.ACKED,
                        broker_order_id=broker_order_id,
                    )
                except order_store.OrderStateTransitionError as exc:
                    LOGGER.exception(
                        "order intent ack failed",
                        extra={
                            "intent_id": intent.intent_id,
                            "requested_state": order_store.OrderIntentState.ACKED.value,
                        },
                    )
                    raise OrderRouterError("order state transition failed") from exc
                ORDER_INTENT_TOTAL.labels(state=intent.state.value).inc()
                record_open_intents(order_store.open_intent_count(session))
            logger = get_golden_logger()
            if logger.enabled:
                logger.log(
                    "order_submit",
                    {
                        "intent_id": intent_key,
                        "request_id": client_request_id,
                        "broker_order_id": broker_order_id,
                        "account": account,
                        "venue": venue,
                        "symbol": symbol,
                        "side": side,
                        "type": order_type,
                        "qty": float(qty),
                        "price": float(price) if price is not None else None,
                        "tif": tif,
                        "strategy": strategy,
                        "reduce_only": bool(reduce_only_flag),
                    },
                )
            return OrderRef(
                intent_id=intent_key,
                request_id=client_request_id,
                broker_order_id=broker_order_id,
                state=order_store.OrderIntentState.ACKED,
            )

    async def cancel_order(
        self,
        *,
        account: str,
        venue: str,
        broker_order_id: str,
        request_id: str | None = None,
        reason: str | None = None,
    ) -> Dict[str, Any]:
        cancel_id = request_id or generate_request_id()
        async with locks.order_lock(account, venue, broker_order_id):
            with order_store.session_scope() as session:
                intent = order_store.ensure_cancel_intent(
                    session,
                    intent_id=cancel_id,
                    request_id=cancel_id,
                    broker_order_id=broker_order_id,
                    account=account,
                    venue=venue,
                    reason=reason,
                )
                if intent.state == order_store.CancelIntentState.ACKED:
                    IDEMPOTENCY_HIT_TOTAL.labels(operation="cancel").inc()
                    return {"status": "canceled", "broker_order_id": broker_order_id}
                order_store.update_cancel_state(
                    session, intent, state=order_store.CancelIntentState.SENT
                )

            try:
                await self._broker.cancel(venue=venue, order_id=broker_order_id)
            except Exception as exc:  # pragma: no cover - broker errors propagate
                LOGGER.info(
                    "cancel failed", extra={"order_id": broker_order_id, "error": str(exc)}
                )
                with order_store.session_scope() as session:
                    intent = order_store.ensure_cancel_intent(
                        session,
                        intent_id=cancel_id,
                        request_id=cancel_id,
                        broker_order_id=broker_order_id,
                        account=account,
                        venue=venue,
                        reason=reason,
                    )
                    order_store.update_cancel_state(
                        session, intent, state=order_store.CancelIntentState.REJECTED
                    )
                raise OrderRouterError("cancel failed") from exc
            with order_store.session_scope() as session:
                intent = order_store.ensure_cancel_intent(
                    session,
                    intent_id=cancel_id,
                    request_id=cancel_id,
                    broker_order_id=broker_order_id,
                    account=account,
                    venue=venue,
                    reason=reason,
                )
                order_store.update_cancel_state(
                    session, intent, state=order_store.CancelIntentState.ACKED
                )
            return {"status": "canceled", "broker_order_id": broker_order_id}

    async def replace_order(
        self,
        *,
        account: str,
        venue: str,
        broker_order_id: str,
        new_params: Mapping[str, Any],
        request_id: str | None = None,
    ) -> OrderRef:
        replacement_id = request_id or generate_request_id()
        with order_store.session_scope() as session:
            existing = order_store.load_intent_by_broker_id(session, broker_order_id)
            if existing is None:
                raise OrderRouterError("cannot replace unknown order")
            if existing.replaced_by:
                replacement = order_store.load_intent(session, existing.replaced_by)
                if replacement and replacement.state in order_store.TERMINAL_STATES:
                    IDEMPOTENCY_HIT_TOTAL.labels(operation="replace").inc()
                    return OrderRef(
                        intent_id=replacement.intent_id,
                        request_id=replacement.request_id,
                        broker_order_id=replacement.broker_order_id,
                        state=replacement.state,
                    )
            symbol = existing.symbol
            side = existing.side
            base_type = existing.type
            base_qty = existing.qty
            base_tif = existing.tif
            base_strategy = existing.strategy

        async with locks.order_lock(account, venue, broker_order_id):
            with order_store.session_scope() as session:
                existing = order_store.load_intent_by_broker_id(session, broker_order_id)
                if existing is None:
                    raise OrderRouterError("cannot replace unknown order")
                order_store.ensure_order_intent(
                    session,
                    intent_id=replacement_id,
                    request_id=replacement_id,
                    account=account,
                    venue=venue,
                    symbol=symbol,
                    side=side,
                    order_type=str(new_params.get("type", base_type)),
                    qty=float(new_params.get("qty", base_qty)),
                    price=float(new_params.get("price")) if new_params.get("price") is not None else None,
                    tif=new_params.get("tif") or base_tif,
                    strategy=new_params.get("strategy") or base_strategy,
                    replaced_by=None,
                )
                existing.replaced_by = replacement_id
                try:
                    order_store.update_intent_state(
                        session,
                        existing,
                        state=order_store.OrderIntentState.REPLACED,
                    )
                except order_store.OrderStateTransitionError as exc:
                    LOGGER.exception(
                        "replace order transition failed",
                        extra={
                            "intent_id": existing.intent_id,
                            "requested_state": order_store.OrderIntentState.REPLACED.value,
                        },
                    )
                    raise OrderRouterError("order state transition failed") from exc

        async with locks.symbol_lock(account, venue, symbol, side):
            new_ref = await self.submit_order(
                account=account,
                venue=venue,
                symbol=symbol,
                side=side,
                order_type=str(new_params.get("type", base_type)),
                qty=float(new_params.get("qty", base_qty)),
                price=new_params.get("price"),
                tif=new_params.get("tif") or base_tif,
                strategy=new_params.get("strategy") or base_strategy,
                request_id=replacement_id,
            )

        await self.cancel_order(
            account=account,
            venue=venue,
            broker_order_id=broker_order_id,
            reason="replace",
        )

        with order_store.session_scope() as session:
            intent = order_store.load_intent(session, new_ref.intent_id)
            if intent:
                chain_length = _replacement_depth(session, intent.intent_id)
                observe_replace_chain(intent.intent_id, chain_length)
                ORDER_INTENT_TOTAL.labels(state=intent.state.value).inc()
                record_open_intents(order_store.open_intent_count(session))
        return new_ref

    async def recover_inflight(self) -> None:
        with order_store.session_scope() as session:
            inflight_ids = [intent.intent_id for intent in order_store.inflight_intents(session)]
        for intent_id in inflight_ids:
            async with locks.intent_lock(intent_id):
                with order_store.session_scope() as session:
                    current = order_store.load_intent(session, intent_id)
                    if current is None:
                        continue
                    venue = current.venue
                    symbol = current.symbol
                    ledger_request_id = order_store.active_request_id(session, intent_id)
                    history = order_store.request_id_history(session, intent_id, limit=5)
                    request_candidates: list[str] = []

                    def _add_candidate(candidate: str | None) -> None:
                        if candidate and candidate not in request_candidates:
                            request_candidates.append(candidate)

                    _add_candidate(current.request_id)
                    _add_candidate(ledger_request_id)
                    for candidate in history:
                        _add_candidate(candidate)
                if not request_candidates:
                    continue
                info: Mapping[str, Any] | None = None
                matched_request_id: str | None = None
                for candidate in request_candidates:
                    try:
                        lookup = await self._broker.get_order_by_client_id(candidate)
                    except Exception as exc:  # pragma: no cover - broker exceptions
                        LOGGER.exception(
                            "recover inflight lookup failed",
                            extra={
                                "event": "recover_inflight_lookup_failed",
                                "intent_id": intent_id,
                                "request_id": candidate,
                                "venue": venue,
                                "symbol": symbol,
                                "error": str(exc),
                            },
                        )
                        continue
                    if isinstance(lookup, Mapping) and lookup:
                        info = lookup
                        matched_request_id = candidate
                        if candidate != request_candidates[0]:
                            LOGGER.info(
                                "recover inflight fallback matched request",
                                extra={
                                    "event": "recover_inflight_fallback",
                                    "intent_id": intent_id,
                                    "request_id": candidate,
                                    "original_request_id": request_candidates[0],
                                    "venue": venue,
                                    "symbol": symbol,
                                },
                            )
                        break
                if not isinstance(info, Mapping) or not info:
                    LOGGER.warning(
                        "inflight order missing on venue",
                        extra={
                            "event": "recover_inflight_missing",
                            "intent_id": intent_id,
                            "request_id": request_candidates[0],
                            "venue": venue,
                            "symbol": symbol,
                            "attempted_request_ids": request_candidates,
                        },
                    )
                    with order_store.session_scope() as session:
                        current = order_store.load_intent(session, intent_id)
                        if current and current.state not in order_store.TERMINAL_STATES:
                            try:
                                order_store.update_intent_state(
                                    session,
                                    current,
                                    state=order_store.OrderIntentState.EXPIRED,
                                )
                            except order_store.OrderStateTransitionError:
                                LOGGER.exception(
                                    "failed to expire missing inflight intent",
                                    extra={
                                        "event": "recover_inflight_expire_failed",
                                        "intent_id": intent_id,
                                        "venue": venue,
                                        "symbol": symbol,
                                    },
                                )
                    continue
                broker_order_id = _extract_order_id(info) or None
                with order_store.session_scope() as session:
                    current = order_store.load_intent(session, intent_id)
                    if not current:
                        continue
                    if matched_request_id:
                        order_store.ensure_active_request(session, current, matched_request_id)
                    (
                        target_state,
                        filled_qty,
                        remaining_qty,
                        avg_price,
                        lifecycle_event,
                    ) = _resolve_remote_state(current, info)
                    if lifecycle_event == "duplicate_fill_ignored":
                        LOGGER.info(
                            "duplicate fill ignored",
                            extra={
                                "event": lifecycle_event,
                                "intent_id": intent_id,
                                "request_id": matched_request_id or request_candidates[0],
                                "broker_order_id": broker_order_id or current.broker_order_id,
                                "venue": venue,
                                "symbol": symbol,
                                "fill_id": info.get("fill_id") or info.get("trade_id"),
                            },
                        )
                    try:
                        updated_intent = order_store.update_intent_state(
                            session,
                            current,
                            state=target_state,
                            broker_order_id=broker_order_id or current.broker_order_id,
                            filled_qty=filled_qty,
                            remaining_qty=remaining_qty,
                            avg_fill_price=avg_price,
                        )
                    except order_store.OrderStateTransitionError:
                        LOGGER.exception(
                            "recovery state transition failed",
                            extra={
                                "event": "recover_inflight_state_failed",
                                "intent_id": intent_id,
                                "state": target_state.value,
                                "request_id": matched_request_id or request_candidates[0],
                                "venue": venue,
                                "symbol": symbol,
                            },
                        )
                        continue
                    ORDER_INTENT_TOTAL.labels(state=updated_intent.state.value).inc()
                    record_open_intents(order_store.open_intent_count(session))


def _extract_order_id(payload: Mapping[str, Any] | None) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("broker_order_id", "order_id", "id"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def _replacement_depth(session, intent_id: str) -> int:
    current = order_store.load_intent(session, intent_id)
    seen = set()
    depth = 0
    while current and current.replaced_by and current.replaced_by not in seen:
        seen.add(current.replaced_by)
        current = order_store.load_intent(session, current.replaced_by)
        depth += 1
    return depth + 1


__all__ = ["OrderRouter", "OrderRef", "OrderRouterError", "enforce_reduce_only"]

