from __future__ import annotations

"""Order-centric quantisation helpers."""

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from ..rules.pretrade import PretradeRejection
from ..util.quantization import QuantizationError, ensure_order_quantized

_ALLOWED_SIDES = {"buy", "sell", "long", "short"}


def as_dec(
    value: Any,
    *,
    field: str = "value",
    allow_none: bool = False,
) -> Decimal | None:
    """Convert *value* into :class:`~decimal.Decimal` with strict validation."""

    if value is None:
        if allow_none:
            return None
        raise PretradeRejection(f"{field}_missing", details={"field": field})
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise PretradeRejection(f"{field}_invalid", details={"field": field}) from exc


def _meta_value(meta: Mapping[str, Any] | Any, *candidates: str) -> Any:
    for key in candidates:
        if isinstance(meta, Mapping):
            if key in meta:
                return meta[key]
        else:
            if hasattr(meta, key):
                return getattr(meta, key)
    return None


def quantize_order(
    side: str,
    price: Decimal | None,
    qty: Decimal,
    meta: Mapping[str, Any] | Any,
) -> tuple[Decimal | None, Decimal]:
    """Quantise *price* and *qty* using venue metadata."""

    side_value = str(side or "").strip().lower()
    if side_value not in _ALLOWED_SIDES:
        raise PretradeRejection("side_invalid", details={"side": side})
    if qty is None:
        raise PretradeRejection("qty_missing", details={"field": "qty"})
    if qty <= 0:
        raise PretradeRejection("qty_invalid", details={"qty": str(qty)})

    step_size = _meta_value(meta, "lot", "step_size")
    tick_size = _meta_value(meta, "tick", "tick_size")
    min_notional = _meta_value(meta, "min_notional")
    min_qty = _meta_value(meta, "min_qty")

    try:
        quantized_qty, quantized_price = ensure_order_quantized(
            qty=qty,
            price=price,
            step_size=step_size,
            tick_size=tick_size,
            min_notional=min_notional,
            min_qty=min_qty,
        )
    except QuantizationError as exc:
        details = {
            "field": exc.field,
            "limit": str(exc.limit) if exc.limit is not None else None,
            "value": str(exc.value) if exc.value is not None else None,
        }
        raise PretradeRejection(exc.reason, details=details) from exc

    return quantized_price, quantized_qty


__all__ = ["as_dec", "quantize_order"]
