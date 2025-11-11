"""Utilities for enforcing venue quantisation constraints."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any


_EPSILON = Decimal("1e-12")


@dataclass(frozen=True, slots=True)
class QuantizationError(ValueError):
    """Raised when an order violates exchange quantisation rules."""

    reason: str
    field: str | None = None
    value: Decimal | None = None
    limit: Decimal | None = None

    def __post_init__(self) -> None:
        message = self.args[0] if self.args else self.reason
        if not message:
            object.__setattr__(self, "args", (self.reason,))
        elif message != self.reason:
            object.__setattr__(self, "args", (message,))


def _to_decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise QuantizationError(f"{field}_invalid", field=field) from exc


def _to_positive_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        dec_value = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):  # pragma: no cover - defensive
        return None
    if dec_value <= 0:
        return None
    return dec_value


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    scaled = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return scaled * step


def _differs(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) > _EPSILON


def ensure_order_quantized(
    *,
    qty: Any,
    price: Any | None,
    step_size: Any | None,
    tick_size: Any | None,
    min_notional: Any | None = None,
    min_qty: Any | None = None,
) -> tuple[Decimal, Decimal | None]:
    """Validate and normalise order parameters against venue constraints."""

    qty_dec = _to_decimal(qty, "qty")
    if qty_dec <= 0:
        raise QuantizationError("qty_invalid", field="qty", value=qty_dec)

    step_dec = _to_positive_decimal(step_size)
    if step_dec is not None:
        floored_qty = _floor_to_step(qty_dec, step_dec)
        if floored_qty <= 0:
            raise QuantizationError(
                "qty_below_step", field="qty", value=qty_dec, limit=step_dec
            )
        if _differs(floored_qty, qty_dec):
            raise QuantizationError(
                "qty_step", field="qty", value=qty_dec, limit=step_dec
            )
    min_qty_dec = _to_positive_decimal(min_qty)
    if min_qty_dec is not None and qty_dec + _EPSILON < min_qty_dec:
        raise QuantizationError(
            "min_qty", field="qty", value=qty_dec, limit=min_qty_dec
        )

    price_dec: Decimal | None = None
    if price is not None:
        price_dec = _to_decimal(price, "price")
        if price_dec <= 0:
            raise QuantizationError("price_invalid", field="price", value=price_dec)
        tick_dec = _to_positive_decimal(tick_size)
        if tick_dec is not None:
            floored_price = _floor_to_step(price_dec, tick_dec)
            if floored_price <= 0:
                raise QuantizationError(
                    "price_below_tick", field="price", value=price_dec, limit=tick_dec
                )
            if _differs(floored_price, price_dec):
                raise QuantizationError(
                    "price_tick", field="price", value=price_dec, limit=tick_dec
                )

    min_notional_dec = _to_positive_decimal(min_notional)
    if min_notional_dec is not None and price_dec is not None:
        notional = qty_dec * price_dec
        if notional + _EPSILON < min_notional_dec:
            raise QuantizationError(
                "min_notional", field="notional", value=notional, limit=min_notional_dec
            )

    return qty_dec, price_dec


__all__ = ["QuantizationError", "ensure_order_quantized"]
