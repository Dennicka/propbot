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

        intent_id = request_id or generate_request_id()
        async with locks.intent_lock(intent_id):
            with order_store.session_scope() as session:
                intent = order_store.ensure_order_intent(
                    session,
                    intent_id=intent_id,
                    request_id=intent_id,
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
                if intent.state in order_store.TERMINAL_STATES and intent.broker_order_id:
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
                if intent.state == order_store.OrderIntentState.SENT:
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
                    idemp_key=intent_id,
                    reduce_only=reduce_only_flag,
                )
            except Exception as exc:  # pragma: no cover - broker errors propagate
                LOGGER.exception(
                    "order submit failed",
                    extra={"intent_id": intent_id, "venue": venue, "error": str(exc)},
                )
                with order_store.session_scope() as session:
                    intent = order_store.load_intent(session, intent_id)
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
                intent = order_store.load_intent(session, intent_id)
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
                intent_id=intent_id,
                request_id=intent_id,
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

