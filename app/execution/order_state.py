"""Order state machine with idempotent exchange updates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping


class OrderStatus(str, Enum):
    """Normalised set of order lifecycle statuses."""

    NEW = "NEW"
    PENDING = "PENDING"
    ACK = "ACK"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


_FINAL_STATUSES = {
    OrderStatus.FILLED,
    OrderStatus.CANCELED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
}

_ACK_ALIASES = {
    "ack",
    "accepted",
    "live",
    "open",
}
_PENDING_ALIASES = {
    "pending",
    "enroute",
    "new",
    "created",
}
_PARTIAL_ALIASES = {
    "partial",
    "partial_fill",
    "partially_filled",
    "partial_filled",
}
_FILLED_ALIASES = {
    "filled",
    "done",
    "completed",
    "closed",
}
_CANCEL_ALIASES = {
    "cancelled",
    "canceled",
    "user_canceled",
    "cancelled_by_user",
    "cancelled_by_exchange",
}
_REJECT_ALIASES = {
    "rejected",
    "failed",
    "error",
}
_EXPIRE_ALIASES = {
    "expired",
    "dead",
    "abandoned",
    "timeout",
}

_FLOAT_KEYS = (
    "qty",
    "quantity",
    "orig_qty",
    "origQty",
    "size",
    "amount",
    "orderQty",
    "order_quantity",
)
_CUM_KEYS = (
    "cum_filled",
    "cumFilled",
    "cum_qty",
    "cumQty",
    "filled_qty",
    "filledQty",
    "executed_qty",
    "executedQty",
    "accumulated_qty",
)
_REMAINING_KEYS = (
    "remaining_qty",
    "remaining",
    "leaves_qty",
    "leavesQty",
    "leavesQuantity",
)
_LAST_FILL_KEYS = (
    "last_fill_qty",
    "lastFillQty",
    "fill_qty",
    "fillQty",
    "last_qty",
    "lastQty",
)
_AVG_PRICE_KEYS = (
    "avg_fill_price",
    "avgPrice",
    "avg_price",
    "fill_price",
    "price_avg",
)
_FILL_ID_KEYS = (
    "fill_id",
    "trade_id",
    "last_trade_id",
    "execution_id",
    "match_id",
    "event_id",
)

_EPSILON = 1e-9


@dataclass(frozen=True)
class OrderState:
    """State snapshot tracked locally for idempotent updates."""

    status: OrderStatus = OrderStatus.NEW
    qty: float = 0.0
    cum_filled: float = 0.0
    avg_price: float | None = None
    last_fill_id: str | None = None
    last_event: str | None = None

    def is_terminal(self) -> bool:
        return self.status in _FINAL_STATUSES


def _coerce_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN guard
        return default
    return result


def _normalise_status(value: Any) -> OrderStatus | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in _FILLED_ALIASES:
        return OrderStatus.FILLED
    if text in _CANCEL_ALIASES:
        return OrderStatus.CANCELED
    if text in _REJECT_ALIASES:
        return OrderStatus.REJECTED
    if text in _EXPIRE_ALIASES:
        return OrderStatus.EXPIRED
    if text in _PARTIAL_ALIASES:
        return OrderStatus.PARTIAL
    if text in _ACK_ALIASES:
        return OrderStatus.ACK
    if text in _PENDING_ALIASES:
        return OrderStatus.PENDING
    if text == OrderStatus.ACK.value.lower():
        return OrderStatus.ACK
    if text == OrderStatus.PARTIAL.value.lower():
        return OrderStatus.PARTIAL
    if text == OrderStatus.FILLED.value.lower():
        return OrderStatus.FILLED
    if text == OrderStatus.CANCELED.value.lower():
        return OrderStatus.CANCELED
    if text == OrderStatus.REJECTED.value.lower():
        return OrderStatus.REJECTED
    if text == OrderStatus.EXPIRED.value.lower():
        return OrderStatus.EXPIRED
    if text == OrderStatus.PENDING.value.lower():
        return OrderStatus.PENDING
    if text == OrderStatus.NEW.value.lower():
        return OrderStatus.NEW
    return None


def _extract_qty(update: Mapping[str, Any], fallback: float) -> float:
    for key in _FLOAT_KEYS:
        if key in update:
            value = _coerce_float(update.get(key))
            if value is not None and value > 0:
                return value
    return float(fallback)


def _extract_cum(update: Mapping[str, Any]) -> float | None:
    for key in _CUM_KEYS:
        if key in update:
            value = _coerce_float(update.get(key))
            if value is not None:
                return max(value, 0.0)
    return None


def _extract_remaining(update: Mapping[str, Any]) -> float | None:
    for key in _REMAINING_KEYS:
        if key in update:
            value = _coerce_float(update.get(key))
            if value is not None:
                return max(value, 0.0)
    return None


def _extract_last_fill_qty(update: Mapping[str, Any]) -> float | None:
    for key in _LAST_FILL_KEYS:
        if key in update:
            value = _coerce_float(update.get(key))
            if value is not None:
                return max(value, 0.0)
    return None


def _extract_avg_price(update: Mapping[str, Any], fallback: float | None) -> float | None:
    for key in _AVG_PRICE_KEYS:
        if key in update:
            value = _coerce_float(update.get(key))
            if value is not None and value > 0:
                return value
    return fallback


def _extract_fill_id(update: Mapping[str, Any]) -> str | None:
    for key in _FILL_ID_KEYS:
        if key in update:
            value = update.get(key)
            if value is not None:
                text = str(value).strip()
                if text:
                    return text
    return None


def _coerce_state(local_state: OrderState | Mapping[str, Any]) -> OrderState:
    if isinstance(local_state, OrderState):
        return local_state
    if isinstance(local_state, Mapping):
        status = (
            _normalise_status(local_state.get("status") or local_state.get("state"))
            or OrderStatus.NEW
        )
        qty = _coerce_float(local_state.get("qty"), 0.0) or 0.0
        cum_filled = _coerce_float(local_state.get("cum_filled"), 0.0) or 0.0
        avg_price = _coerce_float(local_state.get("avg_price"))
        last_fill_id = local_state.get("last_fill_id")
        if last_fill_id is not None:
            last_fill_id = str(last_fill_id)
        return OrderState(
            status=status,
            qty=qty,
            cum_filled=max(cum_filled, 0.0),
            avg_price=avg_price,
            last_fill_id=last_fill_id,
        )
    raise TypeError("local_state must be OrderState or mapping")


def apply_exchange_update(
    local_state: OrderState | Mapping[str, Any],
    update: Mapping[str, Any] | None,
) -> OrderState:
    """Return the new local order state after applying an exchange update.

    The function is idempotent – repeated application of the same ``update`` does
    not change the resulting state or double count fills.
    """

    state = _coerce_state(local_state)
    state = replace(state, last_event=None)
    if update is None:
        return state
    if not isinstance(update, Mapping):
        return state

    qty = _extract_qty(update, state.qty)
    avg_price = _extract_avg_price(update, state.avg_price)
    status_hint = _normalise_status(
        update.get("status") or update.get("state") or update.get("order_status")
    )
    fill_id = _extract_fill_id(update)
    cum_from_update = _extract_cum(update)
    remaining_qty = _extract_remaining(update)
    incremental_fill = _extract_last_fill_qty(update)

    new_cum = state.cum_filled
    duplicate_fill = False

    if fill_id and state.last_fill_id and fill_id == state.last_fill_id:
        duplicate_fill = True

    if cum_from_update is not None:
        if cum_from_update <= state.cum_filled + _EPSILON:
            duplicate_fill = True
        else:
            new_cum = max(cum_from_update, state.cum_filled)
    elif remaining_qty is not None and qty > 0:
        derived_cum = max(qty - remaining_qty, 0.0)
        if derived_cum <= state.cum_filled + _EPSILON:
            duplicate_fill = True
        else:
            new_cum = max(derived_cum, state.cum_filled)
    elif incremental_fill is not None and incremental_fill > _EPSILON:
        if duplicate_fill:
            new_cum = state.cum_filled
        else:
            new_cum = state.cum_filled + incremental_fill

    if new_cum < state.cum_filled:
        new_cum = state.cum_filled

    if not fill_id:
        fill_id = state.last_fill_id
    elif duplicate_fill:
        fill_id = state.last_fill_id

    new_status = state.status
    if not state.is_terminal():
        if status_hint:
            if status_hint == OrderStatus.FILLED:
                if not (duplicate_fill and state.cum_filled + _EPSILON < qty):
                    new_status = OrderStatus.FILLED
            elif status_hint in _FINAL_STATUSES:
                new_status = status_hint
            elif status_hint == OrderStatus.ACK:
                if state.status in {OrderStatus.NEW, OrderStatus.PENDING}:
                    new_status = OrderStatus.ACK
            elif status_hint == OrderStatus.PENDING:
                if state.status == OrderStatus.NEW:
                    new_status = OrderStatus.PENDING
            elif status_hint == OrderStatus.PARTIAL:
                new_status = OrderStatus.PARTIAL

        if new_status not in _FINAL_STATUSES:
            if qty > 0 and new_cum >= qty - _EPSILON:
                new_status = OrderStatus.FILLED
            elif new_cum > _EPSILON:
                new_status = OrderStatus.PARTIAL
            elif status_hint == OrderStatus.ACK:
                new_status = OrderStatus.ACK

    last_event = "duplicate_fill_ignored" if duplicate_fill else None

    return OrderState(
        status=new_status,
        qty=qty,
        cum_filled=new_cum,
        avg_price=avg_price,
        last_fill_id=fill_id,
        last_event=last_event,
    )


__all__ = ["OrderState", "OrderStatus", "apply_exchange_update"]
