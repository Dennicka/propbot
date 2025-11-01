"""Idempotent order router with persistent intent ledger."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Mapping

from ..metrics import (
    IDEMPOTENCY_HIT_TOTAL,
    ORDER_INTENT_TOTAL,
    ORDER_SUBMIT_LATENCY,
    observe_replace_chain,
    record_open_intents,
)
from ..persistence import order_store
from ..runtime import locks
from ..utils.identifiers import generate_request_id


LOGGER = logging.getLogger(__name__)


class OrderRouterError(RuntimeError):
    """Raised when the order router encounters an unrecoverable error."""


@dataclass(slots=True)
class OrderRef:
    intent_id: str
    request_id: str
    broker_order_id: str | None
    state: order_store.OrderIntentState


class OrderRouter:
    def __init__(self, broker) -> None:
        self._broker = broker

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


__all__ = ["OrderRouter", "OrderRef", "OrderRouterError"]

