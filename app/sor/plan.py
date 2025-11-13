from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class Leg:
    venue: str
    symbol: str
    side: str  # "long"|"short"
    qty: Decimal
    px_limit: Decimal
    intent_key: str


@dataclass
class RoutePlan:
    kind: str  # "xarb-perp"
    legs: list[Leg]
    edge_bps: float
    notional_usd: Decimal
    reason: str = "ok"
