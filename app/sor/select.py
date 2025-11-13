from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Dict, Optional, Tuple
import json
import math
import os
import time

from .plan import Leg, RoutePlan


@dataclass
class Quote:
    venue: str
    symbol: str
    bid: Decimal
    ask: Decimal
    ts_ms: int


def _env_json(name: str, default: Dict[str, float]) -> Dict[str, float]:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return json.loads(raw) or default
    except (json.JSONDecodeError, TypeError, ValueError):
        return default


class ScoreCalculator:
    def __init__(self) -> None:
        self.min_edge_bps = float(os.environ.get("SOR_MIN_EDGE_BPS", "1.5"))
        self.min_size_usd = Decimal(os.environ.get("SOR_MIN_SIZE_USD", "50"))
        self.max_slip_bps = float(os.environ.get("SOR_MAX_SLIPPAGE_BPS", "5"))
        self.quote_ttl_ms = int(os.environ.get("SOR_QUOTE_TTL_MS", "200"))
        self.fees_bps = _env_json("SOR_FEES_BPS", {"binance": 2.5, "okx": 2.5, "bybit": 3.0})
        self.funding_1h_bps = _env_json(
            "SOR_FUNDING_BPS_1H", {"binance": 0.0, "okx": 0.0, "bybit": 0.0}
        )
        self.venue_prefs = _env_json("SOR_VENUE_PREFS", {"binance": 1.0, "okx": 1.0, "bybit": 1.0})
        self.edge_ref = os.environ.get("SOR_EDGE_REF", "mid").lower()

    def _ref_price(self, q_long: Quote, q_short: Quote) -> Decimal:
        if self.edge_ref == "ask":
            return q_long.ask
        if self.edge_ref == "bid":
            return q_short.bid
        return (q_long.ask + q_short.bid) / Decimal("2")

    def score_pair(
        self, q_long: Quote, q_short: Quote, notional_usd: Decimal
    ) -> Tuple[float, float]:
        ref = self._ref_price(q_long, q_short)
        if ref <= 0:
            return (-math.inf, 0.0)
        edge_raw_bps = float((q_short.bid - q_long.ask) / ref * Decimal("1e4"))
        fees = float(self.fees_bps.get(q_long.venue, 3.0) + self.fees_bps.get(q_short.venue, 3.0))
        funding = float(
            self.funding_1h_bps.get(q_long.venue, 0.0) - self.funding_1h_bps.get(q_short.venue, 0.0)
        )
        pref = float(
            min(
                self.venue_prefs.get(q_long.venue, 1.0),
                self.venue_prefs.get(q_short.venue, 1.0),
            )
        )
        net = (edge_raw_bps - fees - funding) * pref
        return (net, self.max_slip_bps)


def select_best_pair(
    quotes: Dict[str, Quote], symbol: str, notional_usd: Decimal
) -> Tuple[Optional[RoutePlan], str]:
    now_ms = int(time.time() * 1000)
    sc = ScoreCalculator()
    live = [
        q for q in quotes.values() if q.symbol == symbol and (now_ms - q.ts_ms) <= sc.quote_ttl_ms
    ]
    if len(live) < 2:
        return None, "no-quotes"

    best: Tuple[float, Optional[Quote], Optional[Quote]] = (-math.inf, None, None)
    for ql in live:
        for qs in live:
            if ql.venue == qs.venue:
                continue
            edge_bps, _slip = sc.score_pair(ql, qs, notional_usd)
            if edge_bps > best[0]:
                best = (edge_bps, ql, qs)

    if best[1] is None or best[2] is None:
        return None, "no-pair"

    edge_bps, ql, qs = best
    if edge_bps < sc.min_edge_bps:
        return None, "edge-too-small"

    ref = (ql.ask + qs.bid) / Decimal("2")
    if notional_usd < sc.min_size_usd or ref <= 0:
        return None, "insufficient-size"

    qty = (notional_usd / ref).quantize(Decimal("1e-6"), rounding=ROUND_DOWN)

    slip = Decimal(str(sc.max_slip_bps)) / Decimal("1e4")
    px_long = (ql.ask * (Decimal("1") + slip)).quantize(Decimal("1e-6"), rounding=ROUND_DOWN)
    px_short = (qs.bid * (Decimal("1") - slip)).quantize(Decimal("1e-6"), rounding=ROUND_DOWN)

    plan = RoutePlan(
        kind="xarb-perp",
        legs=[
            Leg(
                venue=ql.venue,
                symbol=symbol,
                side="long",
                qty=qty,
                px_limit=px_long,
                intent_key=f"{ql.venue}|{symbol}|long|{qty}",
            ),
            Leg(
                venue=qs.venue,
                symbol=symbol,
                side="short",
                qty=qty,
                px_limit=px_short,
                intent_key=f"{qs.venue}|{symbol}|short|{qty}",
            ),
        ],
        edge_bps=edge_bps,
        notional_usd=notional_usd,
        reason="ok",
    )
    return plan, "ok"
