from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Callable


@dataclass
class Exposure:
    symbol: str
    usd: Decimal
    per_venue: dict[str, Decimal] | None = None


@dataclass
class Quote:
    venue: str
    symbol: str
    bid: Decimal
    ask: Decimal
    ts_ms: int


@dataclass
class HedgeLeg:
    venue: str
    symbol: str
    side: str
    qty: Decimal
    px_limit: Decimal
    intent_key: str


@dataclass
class HedgePlan:
    symbol: str
    notional_usd: Decimal
    legs: list[HedgeLeg]
    reason: str = "ok"


def _env_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return Decimal(default)
    try:
        return Decimal(raw)
    except ArithmeticError:
        return Decimal(default)


class HedgePolicy:
    def __init__(self, *, now_ms: Callable[[], int] | None = None) -> None:
        self._now_ms = now_ms if now_ms is not None else lambda: int(time.time() * 1000)
        self._min_abs_delta = _env_decimal("HEDGE_MIN_ABS_DELTA_USD", "50")
        self._deadband = _env_decimal("HEDGE_DEADBAND_USD", "25")
        self._step = _env_decimal("HEDGE_STEP_USD", "250")
        self._max_notional = _env_decimal("HEDGE_MAX_NOTIONAL_USD", "5000")
        self._slippage_bps = _env_decimal("HEDGE_MAX_SLIPPAGE_BPS", "5")
        self._quote_ttl_ms = max(int(os.getenv("HEDGE_QUOTE_TTL_MS", "300")), 0)
        self._venue_prefs = self._parse_venue_prefs(os.getenv("HEDGE_VENUE_PREFS"))

    def _parse_venue_prefs(self, raw: str | None) -> dict[str, Decimal]:
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        prefs: dict[str, Decimal] = {}
        for venue, value in payload.items():
            try:
                prefs[str(venue)] = Decimal(str(value))
            except ArithmeticError:
                continue
        return prefs

    def build_plan(self, expo: Exposure, quotes: dict[str, Quote]) -> tuple[HedgePlan | None, str]:
        abs_expo = abs(expo.usd)
        if abs_expo < self._min_abs_delta:
            return None, "deadband-min"

        notional_candidate = abs_expo - self._deadband
        if notional_candidate <= 0:
            return None, "deadband-range"
        notional_candidate = min(notional_candidate, self._max_notional)
        if notional_candidate <= 0:
            return None, "deadband-range"
        if self._step <= 0:
            return None, "invalid-step"
        steps = notional_candidate // self._step
        notional = steps * self._step
        if notional <= 0:
            notional = self._step

        side = "sell" if expo.usd > 0 else "buy"
        now_ms = self._now_ms()
        best_quote = self._select_best_quote(side, quotes, now_ms)
        if best_quote is None:
            return None, "no-quotes"

        ref_price = (best_quote.bid + best_quote.ask) / Decimal(2)
        if ref_price <= 0:
            return None, "invalid-quote"
        qty = (notional / ref_price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if qty <= 0:
            return None, "invalid-qty"

        slip_multiplier = (
            (self._slippage_bps / Decimal(10_000)) if self._slippage_bps else Decimal(0)
        )
        if side == "sell":
            limit_price = best_quote.bid * (Decimal(1) - slip_multiplier)
        else:
            limit_price = best_quote.ask * (Decimal(1) + slip_multiplier)

        leg = HedgeLeg(
            venue=best_quote.venue,
            symbol=best_quote.symbol,
            side=side,
            qty=qty,
            px_limit=limit_price,
            intent_key=f"hedge|{best_quote.venue}|{best_quote.symbol}|{side}|{qty}",
        )
        plan = HedgePlan(symbol=expo.symbol, notional_usd=notional, legs=[leg])
        return plan, "ok"

    def _select_best_quote(self, side: str, quotes: dict[str, Quote], now_ms: int) -> Quote | None:
        candidates: list[tuple[Decimal, Quote]] = []
        direction = Decimal(1) if side == "sell" else Decimal(-1)
        for entry in quotes.values():
            if now_ms - entry.ts_ms > self._quote_ttl_ms:
                continue
            pref = self._venue_prefs.get(entry.venue, Decimal(1))
            price = entry.bid if side == "sell" else entry.ask
            score = direction * price * pref
            candidates.append((score, entry))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
