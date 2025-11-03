"""Idempotent order router with persistent intent ledger."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Tuple

from ..audit_log import log_operator_action
from .. import ledger
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
from ..risk.exposure_caps import (
    check_open_allowed,
    collect_snapshot,
    resolve_caps,
    snapshot_entry,
)


LOGGER = logging.getLogger(__name__)


_CRITICAL_CAUSE = "ACCOUNT_HEALTH_CRITICAL"
_REDUCE_ONLY_BLOCK = "blocked: reduce-only due to ACCOUNT_HEALTH::CRITICAL"


def _coerce_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _resolve_gate(ctx: object) -> object | None:
    runtime_scope = getattr(ctx, "runtime", ctx)
    gate = getattr(runtime_scope, "pre_trade_gate", None)
    if gate is not None:
        return gate
    getter = getattr(runtime_scope, "get_pre_trade_gate", None)
    if callable(getter):
        try:
            return getter()
        except TypeError:
            pass
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
        gate = runtime.get_pre_trade_gate()
        allowed, gate_reason = gate.check_allowed(order_payload)
        reduce_only_flag = False
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
                reduce_only_flag = native_supported
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
        client_request_id = request_id or intent_key
        async with locks.intent_lock(intent_key):
            prev_state: order_store.OrderIntentState | None = None
            prev_request_id: str | None = None
            prev_broker_order_id: str | None = None
            with order_store.session_scope() as session:
                existing = order_store.load_intent(session, intent_key)
                if existing is not None:
                    prev_state = existing.state
                    prev_request_id = existing.request_id
                    prev_broker_order_id = existing.broker_order_id
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
                if (
                    prev_state in order_store.TERMINAL_STATES
                    and prev_broker_order_id
                    and prev_request_id == client_request_id
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
                    prev_state == order_store.OrderIntentState.SENT
                    and prev_request_id == client_request_id
                ):
                    IDEMPOTENCY_HIT_TOTAL.labels(operation="submit").inc()
                    return OrderRef(
                        intent_id=intent.intent_id,
                        request_id=intent.request_id,
                        broker_order_id=intent.broker_order_id,
                        state=intent.state,
                    )
                order_store.update_intent_state(
                    session, intent, state=order_store.OrderIntentState.SENT
                )

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
                        order_store.update_intent_state(
                            session, intent, state=order_store.OrderIntentState.REJECTED
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
                order_store.update_intent_state(
                    session,
                    intent,
                    state=order_store.OrderIntentState.ACKED,
                    broker_order_id=broker_order_id,
                )
                ORDER_INTENT_TOTAL.labels(state=intent.state.value).inc()
                record_open_intents(order_store.open_intent_count(session))
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
                existing.state = order_store.OrderIntentState.REPLACED
                session.add(existing)
                session.flush()

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
            inflight = list(order_store.inflight_intents(session))
        for intent in inflight:
            request_id = intent.intent_id
            async with locks.intent_lock(request_id):
                info = await self._broker.get_order_by_client_id(request_id)
                if not info:
                    continue
                broker_order_id = _extract_order_id(info)
                state = order_store.OrderIntentState.ACKED
                with order_store.session_scope() as session:
                    current = order_store.load_intent(session, request_id)
                    if current:
                        order_store.update_intent_state(
                            session,
                            current,
                            state=state,
                            broker_order_id=broker_order_id,
                        )


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

