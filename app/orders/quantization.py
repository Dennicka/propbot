"""Order quantisation helpers using Decimal arithmetic."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Mapping

LOGGER = logging.getLogger(__name__)


def as_dec(value: Any) -> Decimal:
    """Coerce the input into a finite Decimal."""

    if isinstance(value, Decimal):
        if not value.is_finite():
            LOGGER.error(
                "orders.quantization.non_finite",
                extra={
                    "event": "orders_quantization_non_finite",
                    "component": "orders_quantization",
                    "details": {"value": str(value)},
                },
            )
            raise ValueError("non_finite_decimal")
        return value
    if value is None:
        LOGGER.error(
            "orders.quantization.none_value",
            extra={
                "event": "orders_quantization_none_value",
                "component": "orders_quantization",
            },
        )
        raise ValueError("none_value")
    try:
        coerced = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        LOGGER.error(
            "orders.quantization.invalid_decimal",
            extra={
                "event": "orders_quantization_invalid_decimal",
                "component": "orders_quantization",
                "details": {"value": value},
            },
            exc_info=exc,
        )
        raise ValueError("invalid_decimal") from exc
    if not coerced.is_finite():
        LOGGER.error(
            "orders.quantization.non_finite",
            extra={
                "event": "orders_quantization_non_finite",
                "component": "orders_quantization",
                "details": {"value": str(coerced)},
            },
        )
        raise ValueError("non_finite_decimal")
    return coerced.normalize()


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Floor the value to the nearest multiple of ``step``."""

    if step <= 0:
        if step < 0:
            LOGGER.warning(
                "orders.quantization.negative_step",
                extra={
                    "event": "orders_quantization_negative_step",
                    "component": "orders_quantization",
                    "details": {"step": str(step)},
                },
            )
        return value
    try:
        scaled = (value / step).to_integral_value(rounding=ROUND_DOWN)
    except (InvalidOperation, ZeroDivisionError) as exc:
        LOGGER.error(
            "orders.quantization.step_division_failed",
            extra={
                "event": "orders_quantization_step_division_failed",
                "component": "orders_quantization",
                "details": {"value": str(value), "step": str(step)},
            },
            exc_info=exc,
        )
        raise ValueError("step_division_failed") from exc
    floored = (scaled * step).normalize()
    return floored


def quantize_price(price: Decimal, tick: Decimal) -> Decimal:
    """Quantise a price down to the venue tick size."""

    if price < 0:
        LOGGER.error(
            "orders.quantization.negative_price",
            extra={
                "event": "orders_quantization_negative_price",
                "component": "orders_quantization",
                "details": {"price": str(price)},
            },
        )
        raise ValueError("negative_price")
    if tick <= 0:
        if tick < 0:
            LOGGER.warning(
                "orders.quantization.invalid_tick",
                extra={
                    "event": "orders_quantization_invalid_tick",
                    "component": "orders_quantization",
                    "details": {"tick": str(tick)},
                },
            )
        return price
    return floor_to_step(price, tick)


def quantize_qty(qty: Decimal, step: Decimal) -> Decimal:
    """Quantise a quantity down to the venue lot step."""

    if qty < 0:
        LOGGER.error(
            "orders.quantization.negative_qty",
            extra={
                "event": "orders_quantization_negative_qty",
                "component": "orders_quantization",
                "details": {"qty": str(qty)},
            },
        )
        raise ValueError("negative_qty")
    if step <= 0:
        if step < 0:
            LOGGER.warning(
                "orders.quantization.invalid_step",
                extra={
                    "event": "orders_quantization_invalid_step",
                    "component": "orders_quantization",
                    "details": {"step": str(step)},
                },
            )
        return qty
    return floor_to_step(qty, step)


def _meta_decimal(meta: Mapping[str, object], key: str) -> Decimal:
    raw = meta.get(key)
    if raw is None:
        return Decimal("0")
    try:
        value = as_dec(raw)
    except ValueError:
        LOGGER.warning(
            "orders.quantization.meta_invalid",
            extra={
                "event": "orders_quantization_meta_invalid",
                "component": "orders_quantization",
                "details": {"key": key, "value": raw},
            },
        )
        return Decimal("0")
    return value


def quantize_order(
    side: str,
    price: Decimal,
    qty: Decimal,
    meta: Mapping[str, object],
) -> tuple[Decimal, Decimal]:
    """Quantise order parameters according to venue metadata."""

    if price <= 0 or qty <= 0:
        LOGGER.error(
            "orders.quantization.non_positive_input",
            extra={
                "event": "orders_quantization_non_positive_input",
                "component": "orders_quantization",
                "details": {"price": str(price), "qty": str(qty), "side": side},
            },
        )
        raise ValueError("non_positive_input")
    if not isinstance(meta, Mapping):
        LOGGER.warning(
            "orders.quantization.meta_missing",
            extra={
                "event": "orders_quantization_meta_missing",
                "component": "orders_quantization",
                "details": {"side": side},
            },
        )
        meta = {}

    tick = _meta_decimal(meta, "tickSize")
    step = _meta_decimal(meta, "stepSize")
    q_price = quantize_price(price, tick) if tick > 0 else price
    q_qty = quantize_qty(qty, step) if step > 0 else qty
    if q_price <= 0 or q_qty <= 0:
        LOGGER.error(
            "orders.quantization.quantized_non_positive",
            extra={
                "event": "orders_quantization_quantized_non_positive",
                "component": "orders_quantization",
                "details": {"price": str(q_price), "qty": str(q_qty), "side": side},
            },
        )
        raise ValueError("quantized_non_positive")
    return q_price.normalize(), q_qty.normalize()


__all__ = [
    "as_dec",
    "floor_to_step",
    "quantize_order",
    "quantize_price",
    "quantize_qty",
]
