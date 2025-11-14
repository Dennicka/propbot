"""Primitives for estimating trade execution costs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, cast


@dataclass(slots=True)
class TradeCostEstimate:
    """Container for estimated trade costs.

    Attributes:
        venue: Exchange venue identifier.
        symbol: Trading symbol.
        side: Trade direction ("buy" or "sell").
        qty: Trade quantity.
        price: Trade price.
        taker_fee_bps: Taker fee in basis points.
        maker_fee_bps: Maker fee in basis points (reserved for future use).
        estimated_fee: Estimated taker fee amount.
        funding_rate: Funding rate applied to the trade, if any.
        estimated_funding_cost: Estimated funding cost component.
        total_cost: Combined cost (fees + funding).
    """

    venue: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: Decimal
    price: Decimal
    taker_fee_bps: Decimal
    maker_fee_bps: Decimal
    estimated_fee: Decimal
    funding_rate: Decimal | None
    estimated_funding_cost: Decimal
    total_cost: Decimal


def estimate_trade_cost(
    *,
    venue: str,
    symbol: str,
    side: str,
    qty: Decimal,
    price: Decimal,
    taker_fee_bps: Decimal,
    funding_rate: Decimal | None = None,
) -> TradeCostEstimate:
    """Estimate trade costs for the provided order parameters."""

    normalised_side = side.lower()
    if normalised_side not in {"buy", "sell"}:
        raise ValueError("side must be either 'buy' or 'sell'")

    if qty <= 0:
        raise ValueError("qty must be positive")

    if price <= 0:
        raise ValueError("price must be positive")

    maker_fee_bps = Decimal("0")
    fee = (qty * price * taker_fee_bps) / Decimal("10000")

    if funding_rate is None:
        estimated_funding_cost = Decimal("0")
    else:
        estimated_funding_cost = qty * price * funding_rate

    total_cost = fee + estimated_funding_cost

    side_literal = cast(Literal["buy", "sell"], normalised_side)

    return TradeCostEstimate(
        venue=venue,
        symbol=symbol,
        side=side_literal,
        qty=qty,
        price=price,
        taker_fee_bps=taker_fee_bps,
        maker_fee_bps=maker_fee_bps,
        estimated_fee=fee,
        funding_rate=funding_rate,
        estimated_funding_cost=estimated_funding_cost,
        total_cost=total_cost,
    )
