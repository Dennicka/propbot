from __future__ import annotations

"""Exchange metadata normalisation helpers."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


@dataclass(frozen=True)
class SymbolMeta:
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal | None = None
    min_qty: Decimal | None = None


def as_dec(value: Any) -> Decimal:
    """Convert *value* to :class:`~decimal.Decimal` without using float."""

    if isinstance(value, Decimal):
        return value
    if value is None:
        raise ValueError("value is None")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"invalid decimal value: {value!r}") from exc


def _get_mapping(value: Mapping[str, Any] | Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise TypeError("metadata payload must be a mapping")


def normalize_binance(raw: Mapping[str, Any] | Any) -> SymbolMeta:
    """Normalise Binance Futures metadata payload."""

    payload = _get_mapping(raw)
    filters = payload.get("filters", [])
    tick_size: Decimal | None = None
    step_size: Decimal | None = None
    min_notional: Decimal | None = None
    min_qty: Decimal | None = None
    for entry in filters:
        if not isinstance(entry, Mapping):
            continue
        filter_type = entry.get("filterType")
        if filter_type == "PRICE_FILTER":
            tick_size = as_dec(entry.get("tickSize"))
        elif filter_type == "LOT_SIZE":
            step_size = as_dec(entry.get("stepSize"))
            min_qty_raw = entry.get("minQty")
            if min_qty_raw is not None:
                min_qty = as_dec(min_qty_raw)
        elif filter_type == "MIN_NOTIONAL":
            min_notional_raw = entry.get("notional")
            if min_notional_raw is not None:
                min_notional = as_dec(min_notional_raw)
    if tick_size is None:
        raise ValueError("binance payload missing PRICE_FILTER.tickSize")
    if step_size is None:
        raise ValueError("binance payload missing LOT_SIZE.stepSize")
    return SymbolMeta(
        tick_size=tick_size,
        step_size=step_size,
        min_notional=min_notional,
        min_qty=min_qty,
    )


def normalize_okx(raw: Mapping[str, Any] | Any) -> SymbolMeta:
    """Normalise OKX perpetual metadata payload."""

    payload = _get_mapping(raw)
    tick_size = as_dec(payload.get("tickSz"))
    step_size = as_dec(payload.get("lotSz"))
    min_qty_raw = payload.get("minSz")
    min_qty = as_dec(min_qty_raw) if min_qty_raw is not None else None
    min_notional_raw = payload.get("minNotional") or payload.get("minNotionalValue")
    if min_notional_raw is None:
        ct_val = payload.get("ctVal")
        if ct_val is not None and min_qty is not None:
            min_notional_raw = as_dec(ct_val) * min_qty
    min_notional = None
    if min_notional_raw is not None:
        min_notional = as_dec(min_notional_raw)
    return SymbolMeta(
        tick_size=tick_size,
        step_size=step_size,
        min_notional=min_notional,
        min_qty=min_qty,
    )


def normalize_bybit(raw: Mapping[str, Any] | Any) -> SymbolMeta:
    """Normalise Bybit perpetual metadata payload."""

    payload = _get_mapping(raw)
    price_filter = payload.get("priceFilter", {})
    lot_filter = payload.get("lotSizeFilter", {})
    if not isinstance(price_filter, Mapping):
        raise ValueError("bybit payload missing priceFilter mapping")
    if not isinstance(lot_filter, Mapping):
        raise ValueError("bybit payload missing lotSizeFilter mapping")
    tick_size = as_dec(price_filter.get("tickSize"))
    step_size = as_dec(lot_filter.get("qtyStep"))
    min_qty_raw = lot_filter.get("minQty") or lot_filter.get("minTradingQty")
    min_qty = as_dec(min_qty_raw) if min_qty_raw is not None else None
    min_notional_raw = payload.get("minNotional") or payload.get("minTradingValue")
    min_notional = as_dec(min_notional_raw) if min_notional_raw is not None else None
    return SymbolMeta(
        tick_size=tick_size,
        step_size=step_size,
        min_notional=min_notional,
        min_qty=min_qty,
    )


class MetadataProvider:
    """In-memory cache for symbol metadata."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], SymbolMeta] = {}

    def put(self, venue: str, symbol: str, meta: SymbolMeta) -> None:
        key = (str(venue).lower(), str(symbol).upper())
        self._cache[key] = meta

    def get(self, venue: str, symbol: str) -> SymbolMeta | None:
        key = (str(venue).lower(), str(symbol).upper())
        return self._cache.get(key)

    def clear(self) -> None:
        self._cache.clear()


provider = MetadataProvider()


__all__ = [
    "SymbolMeta",
    "as_dec",
    "normalize_binance",
    "normalize_okx",
    "normalize_bybit",
    "MetadataProvider",
    "provider",
]
