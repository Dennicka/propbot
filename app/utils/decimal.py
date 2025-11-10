"""Helpers for safe :class:`~decimal.Decimal` usage in monetary calculations."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, Context, InvalidOperation, ROUND_HALF_EVEN, localcontext
from typing import Iterator, Union

__all__ = ["decimal_context", "to_decimal", "NumberLike"]

_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN, Emin=-28, Emax=28)


@contextmanager
def decimal_context() -> Iterator[None]:
    """Provide a high-precision, money-safe decimal context."""

    with localcontext(_DECIMAL_CONTEXT):
        yield


NumberLike = Union[int, float, str, Decimal]


def to_decimal(value: object, *, default: Decimal | None = None) -> Decimal:
    """Coerce ``value`` into :class:`~decimal.Decimal` safely."""

    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, (int, str)):
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError):
            if default is not None:
                return default
            raise
    if isinstance(value, float):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            if default is not None:
                return default
            raise
    if default is not None:
        return default
    raise TypeError(f"Unsupported value for Decimal conversion: {value!r}")
