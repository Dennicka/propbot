"""Funding-aware venue routing for perpetual derivatives."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Mapping, MutableMapping, Optional

import os

from ..tca.cost_model import effective_cost as tca_effective_cost, funding_bps_per_hour
from ..utils.symbols import normalise_symbol, resolve_venue_symbol


LOGGER = logging.getLogger(__name__)

FUNDING_INTERVAL_SECONDS = 8 * 3600.0


def _feature_flag_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _tca_router_enabled() -> bool:
    return _feature_flag_enabled("FEATURE_TCA_ROUTER", False)


@dataclass(frozen=True)
class VenueQuote:
    """Snapshot of routing inputs for a derivative venue."""

    venue: str
    taker_fee_bps: float
    funding_rate: float
    next_funding_ts: float
    maker_fee_bps: float = 0.0
    vip_rebate_bps: float = 0.0
    maker_possible: bool = False

    @property
    def funding_per_hour_bps(self) -> float:
        return funding_bps_per_hour(self.funding_rate)


@dataclass(frozen=True)
class FundingAdjustment:
    """Effective fee adjustments for a long/short combination."""

    long_venue: str
    short_venue: str
    long_fee_bps: float
    short_fee_bps: float
    horizon_long: float
    horizon_short: float
    total_fee_bps: float
    long_breakdown: Optional[Dict[str, object]] = None
    short_breakdown: Optional[Dict[str, object]] = None


def _compute_horizon(next_funding_ts: float, *, include_next_window: bool, now: float) -> float:
    if not include_next_window:
        return 0.0
    if next_funding_ts <= 0:
        return 1.0
    time_to_next = max(next_funding_ts - now, 0.0)
    if FUNDING_INTERVAL_SECONDS <= 0:
        return 1.0
    return min(time_to_next / FUNDING_INTERVAL_SECONDS, 1.0)


def compute_effective_cost(
    *,
    taker_fee_bps: float,
    funding_rate: float,
    horizon: float,
    side: str,
) -> float:
    """Adjust taker fee (bps) by funding expectations for the given side."""

    side_lower = side.lower()
    funding_bps = float(funding_rate) * 10_000.0 * float(max(horizon, 0.0))
    if side_lower in {"buy", "long"}:
        effective = taker_fee_bps + funding_bps
    elif side_lower in {"sell", "short"}:
        effective = taker_fee_bps - funding_bps
    else:
        raise ValueError("side must be long/short or buy/sell")
    return float(effective)


def effective_fee_for_quote(
    quote: VenueQuote,
    *,
    side: str,
    include_next_window: bool,
    now: float | None = None,
) -> float:
    current_ts = now if now is not None else time.time()
    horizon = _compute_horizon(
        quote.next_funding_ts, include_next_window=include_next_window, now=current_ts
    )
    return compute_effective_cost(
        taker_fee_bps=quote.taker_fee_bps,
        funding_rate=quote.funding_rate,
        horizon=horizon,
        side=side,
    )


def _build_adjustments(
    venues: Mapping[str, VenueQuote],
    *,
    include_next_window: bool,
    now: float,
) -> List[FundingAdjustment]:
    adjustments: List[FundingAdjustment] = []
    venue_items = list(venues.items())
    use_tca = _tca_router_enabled()
    interval_minutes = FUNDING_INTERVAL_SECONDS / 60.0 if FUNDING_INTERVAL_SECONDS else 0.0
    for long_name, long_quote in venue_items:
        for short_name, short_quote in venue_items:
            if long_name == short_name:
                continue
            long_horizon = _compute_horizon(
                long_quote.next_funding_ts, include_next_window=include_next_window, now=now
            )
            short_horizon = _compute_horizon(
                short_quote.next_funding_ts, include_next_window=include_next_window, now=now
            )
            if use_tca:
                long_horizon_min = long_horizon * interval_minutes
                short_horizon_min = short_horizon * interval_minutes
                long_cost = tca_effective_cost(
                    "long",
                    qty=1.0,
                    px=1.0,
                    horizon_min=long_horizon_min,
                    is_maker_possible=long_quote.maker_possible,
                    venue_meta={
                        "venue": long_quote.venue,
                        "fees": {
                            "maker_bps": long_quote.maker_fee_bps,
                            "taker_bps": long_quote.taker_fee_bps,
                            "vip_rebate_bps": long_quote.vip_rebate_bps,
                        },
                        "funding_bps_per_hour": long_quote.funding_per_hour_bps,
                    },
                )
                short_cost = tca_effective_cost(
                    "short",
                    qty=1.0,
                    px=1.0,
                    horizon_min=short_horizon_min,
                    is_maker_possible=short_quote.maker_possible,
                    venue_meta={
                        "venue": short_quote.venue,
                        "fees": {
                            "maker_bps": short_quote.maker_fee_bps,
                            "taker_bps": short_quote.taker_fee_bps,
                            "vip_rebate_bps": short_quote.vip_rebate_bps,
                        },
                        "funding_bps_per_hour": short_quote.funding_per_hour_bps,
                    },
                )
                long_fee = float(long_cost.get("bps", 0.0))
                short_fee = float(short_cost.get("bps", 0.0))
                total = long_fee + short_fee
                long_breakdown = (
                    long_cost.get("breakdown") if isinstance(long_cost, Mapping) else None
                )
                short_breakdown = (
                    short_cost.get("breakdown") if isinstance(short_cost, Mapping) else None
                )
            else:
                long_fee = compute_effective_cost(
                    taker_fee_bps=long_quote.taker_fee_bps,
                    funding_rate=long_quote.funding_rate,
                    horizon=long_horizon,
                    side="long",
                )
                short_fee = compute_effective_cost(
                    taker_fee_bps=short_quote.taker_fee_bps,
                    funding_rate=short_quote.funding_rate,
                    horizon=short_horizon,
                    side="short",
                )
                total = long_fee + short_fee
                long_breakdown = None
                short_breakdown = None
            adjustments.append(
                FundingAdjustment(
                    long_venue=long_name,
                    short_venue=short_name,
                    long_fee_bps=long_fee,
                    short_fee_bps=short_fee,
                    horizon_long=long_horizon,
                    horizon_short=short_horizon,
                    total_fee_bps=total,
                    long_breakdown=long_breakdown,
                    short_breakdown=short_breakdown,
                )
            )
    return adjustments


def choose_best_pair(
    venues: Mapping[str, Mapping[str, float] | VenueQuote],
    *,
    include_next_window: bool = True,
    now: float | None = None,
) -> FundingAdjustment | None:
    """Return the venue combination with the lowest effective fee cost."""

    if not venues:
        return None
    quotes: MutableMapping[str, VenueQuote] = {}
    for venue_name, payload in venues.items():
        if isinstance(payload, VenueQuote):
            quotes[venue_name] = payload
            continue
        taker = float(payload.get("taker_fee_bps", 0.0))
        funding = float(payload.get("funding_rate", 0.0))
        next_ts = float(payload.get("next_funding_ts", 0.0))
        quotes[venue_name] = VenueQuote(
            venue=str(venue_name),
            taker_fee_bps=taker,
            funding_rate=funding,
            next_funding_ts=next_ts,
        )
    current_ts = now if now is not None else time.time()
    adjustments = _build_adjustments(
        quotes, include_next_window=include_next_window, now=current_ts
    )
    if not adjustments:
        return None
    best = min(adjustments, key=lambda adj: adj.total_fee_bps)
    tca_used = any(adj.long_breakdown or adj.short_breakdown for adj in adjustments)
    LOGGER.debug(
        "funding_router evaluated pairs",
        extra={
            "now": current_ts,
            "include_next_window": include_next_window,
            "tca_router": tca_used,
            "pairs": [
                {
                    "long": adj.long_venue,
                    "short": adj.short_venue,
                    "long_fee_bps": round(adj.long_fee_bps, 6),
                    "short_fee_bps": round(adj.short_fee_bps, 6),
                    "total_fee_bps": round(adj.total_fee_bps, 6),
                    "long_mode": (
                        (adj.long_breakdown or {}).get("execution", {}).get("mode")
                        if tca_used
                        else None
                    ),
                    "short_mode": (
                        (adj.short_breakdown or {}).get("execution", {}).get("mode")
                        if tca_used
                        else None
                    ),
                }
                for adj in adjustments
            ],
        },
    )
    return best


def extract_funding_inputs(
    *,
    runtime_state,
    symbol: str,
    venue_alias_map: Mapping[str, str | None],
    include_next_window: bool = True,
) -> Dict[str, VenueQuote]:
    """Collect funding snapshots for the configured venues."""

    derivatives = getattr(runtime_state, "derivatives", None)
    config = getattr(getattr(runtime_state, "config", None), "data", None)
    control = getattr(runtime_state, "control", None)
    if derivatives is None or not getattr(derivatives, "venues", {}):
        return {}
    symbol_norm = normalise_symbol(symbol)
    quotes: Dict[str, VenueQuote] = {}
    now = time.time()
    manual_fees: Dict[str, Dict[str, float]] = {}
    prefer_maker = False
    if config is not None:
        derivatives_cfg = getattr(config, "derivatives", None)
        if derivatives_cfg is not None:
            arbitrage_cfg = getattr(derivatives_cfg, "arbitrage", None)
            if arbitrage_cfg is not None:
                prefer_maker = bool(getattr(arbitrage_cfg, "prefer_maker", False))
            fees_cfg = getattr(derivatives_cfg, "fees", None)
            manual_cfg = getattr(fees_cfg, "manual", None) if fees_cfg else None
            if manual_cfg:
                if isinstance(manual_cfg, Mapping):
                    items = manual_cfg.items()
                else:
                    try:
                        items = manual_cfg.model_dump().items()  # type: ignore[attr-defined]
                    except Exception:  # pragma: no cover - defensive
                        items = []
                for venue_id, payload in items:
                    if isinstance(payload, Mapping):
                        manual_fees[str(venue_id)] = {
                            "maker_bps": float(payload.get("maker_bps", 0.0)),
                            "taker_bps": float(payload.get("taker_bps", 0.0)),
                            "vip_rebate_bps": float(payload.get("vip_rebate_bps", 0.0)),
                        }
    global_maker_possible = prefer_maker or bool(getattr(control, "post_only", False))
    for alias, venue_id in venue_alias_map.items():
        if not venue_id:
            continue
        runtime = derivatives.venues.get(venue_id)
        if runtime is None:
            continue
        client = getattr(runtime, "client", None)
        if client is None:
            continue
        resolved_symbol = resolve_venue_symbol(config, venue_id=venue_id, symbol=symbol) or symbol
        try:
            funding_info = client.get_funding_info(resolved_symbol)
        except Exception:
            continue
        rate = float(funding_info.get("rate", 0.0)) if isinstance(funding_info, Mapping) else 0.0
        next_ts = (
            float(funding_info.get("next_funding_ts", 0.0))
            if isinstance(funding_info, Mapping)
            else 0.0
        )
        manual_entry = manual_fees.get(str(venue_id), {})
        try:
            fee_info = client.get_fees(resolved_symbol)
            taker_bps = float(fee_info.get("taker_bps", manual_entry.get("taker_bps", 0.0)))
            maker_bps = float(fee_info.get("maker_bps", manual_entry.get("maker_bps", 0.0)))
        except Exception:
            taker_bps = float(manual_entry.get("taker_bps", 0.0))
            maker_bps = float(manual_entry.get("maker_bps", 0.0))
        vip_rebate_bps = float(manual_entry.get("vip_rebate_bps", 0.0))
        quotes[str(alias)] = VenueQuote(
            venue=str(alias),
            taker_fee_bps=taker_bps,
            maker_fee_bps=maker_bps,
            vip_rebate_bps=vip_rebate_bps,
            maker_possible=global_maker_possible,
            funding_rate=rate,
            next_funding_ts=next_ts or now,
        )
    if quotes:
        LOGGER.debug(
            "funding_router collected inputs",
            extra={
                "symbol": symbol_norm,
                "venues": {
                    name: {
                        "taker_fee_bps": round(quote.taker_fee_bps, 6),
                        "maker_fee_bps": round(quote.maker_fee_bps, 6),
                        "vip_rebate_bps": round(quote.vip_rebate_bps, 6),
                        "maker_possible": quote.maker_possible,
                        "funding_rate": round(quote.funding_rate, 8),
                        "next_funding_ts": quote.next_funding_ts,
                    }
                    for name, quote in quotes.items()
                },
                "include_next_window": include_next_window,
            },
        )
    return quotes


__all__ = [
    "FundingAdjustment",
    "VenueQuote",
    "choose_best_pair",
    "compute_effective_cost",
    "effective_fee_for_quote",
    "extract_funding_inputs",
]
