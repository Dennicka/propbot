from __future__ import annotations

import logging
import os
import time
from typing import Dict, Iterable, List, Mapping, Tuple

from ..routing.funding_router import VenueQuote, extract_funding_inputs
from ..services.runtime import get_market_data, get_state
from ..tca.cost_model import FeeInfo, FeeTable, ImpactModel, TierTable, effective_cost
from ..utils.symbols import normalise_symbol, resolve_venue_symbol

LOGGER = logging.getLogger(__name__)


def _feature_flag_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def feature_enabled() -> bool:
    return _feature_flag_enabled("FEATURE_TCA_ROUTER", False)


def _manual_fee_table(config) -> FeeTable:
    derivatives_cfg = getattr(config, "derivatives", None)
    fees_cfg = getattr(derivatives_cfg, "fees", None) if derivatives_cfg else None
    manual_cfg = getattr(fees_cfg, "manual", None) if fees_cfg else None
    mapping: Dict[str, Mapping[str, float]] = {}
    if manual_cfg:
        if isinstance(manual_cfg, Mapping):
            items = manual_cfg.items()
        else:
            try:
                items = manual_cfg.model_dump().items()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - defensive guard
                items = []
        for venue, payload in items:
            if isinstance(payload, Mapping):
                mapping[str(venue)] = {
                    "maker_bps": float(payload.get("maker_bps", 0.0)),
                    "taker_bps": float(payload.get("taker_bps", 0.0)),
                    "vip_rebate_bps": float(payload.get("vip_rebate_bps", 0.0)),
                }
    return FeeTable.from_mapping(mapping)


def _tier_table_from_config(config) -> TierTable | None:
    tca_cfg = getattr(config, "tca", None)
    tiers_cfg = getattr(tca_cfg, "tiers", None) if tca_cfg else None
    if not tiers_cfg:
        return None
    mapping: Dict[str, List[Mapping[str, object]]] = {}
    if isinstance(tiers_cfg, Mapping):
        items = tiers_cfg.items()
    else:
        try:
            items = tiers_cfg.model_dump().items()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - defensive
            items = []
    for venue, payload in items:
        tier_entries: List[Mapping[str, object]] = []
        if isinstance(payload, Iterable):
            for entry in payload:
                if isinstance(entry, Mapping):
                    tier_entries.append(entry)
                else:
                    try:
                        tier_entries.append(entry.model_dump())  # type: ignore[attr-defined]
                    except (AttributeError, TypeError, ValueError) as exc:
                        LOGGER.warning(
                            "TCA preview: failed to serialize tier entry %r", entry, exc_info=exc
                        )
                        continue
        if tier_entries:
            mapping[str(venue)] = tier_entries
    if not mapping:
        return None
    return TierTable.from_mapping(mapping)


def _impact_model_from_config(config) -> ImpactModel | None:
    tca_cfg = getattr(config, "tca", None)
    impact_cfg = getattr(tca_cfg, "impact", None) if tca_cfg else None
    if impact_cfg is None:
        return None
    k_value = getattr(impact_cfg, "k", 0.0)
    try:
        k_float = float(k_value)
    except (TypeError, ValueError):
        k_float = 0.0
    return ImpactModel(k=k_float)


def _default_horizon_minutes(config) -> float:
    tca_cfg = getattr(config, "tca", None)
    if tca_cfg is None:
        return 60.0
    horizon_value = getattr(tca_cfg, "horizon_min", None)
    try:
        horizon_float = float(horizon_value)
    except (TypeError, ValueError):
        horizon_float = 60.0
    if horizon_float < 0:
        horizon_float = 0.0
    return horizon_float


def _prefer_maker(config) -> bool:
    derivatives_cfg = getattr(config, "derivatives", None)
    arbitrage_cfg = getattr(derivatives_cfg, "arbitrage", None) if derivatives_cfg else None
    return bool(getattr(arbitrage_cfg, "prefer_maker", False))


def _resolve_leg_symbol(config, venue_id: str, symbol: str, pair_norm: str) -> Tuple[str, str]:
    resolved = resolve_venue_symbol(config, venue_id=venue_id, symbol=symbol)
    if resolved:
        return str(resolved), normalise_symbol(resolved)
    return str(symbol), pair_norm


def _coerce_quote(quotes: Mapping[str, VenueQuote], venue: str) -> VenueQuote | None:
    quote = quotes.get(venue)
    if isinstance(quote, VenueQuote):
        return quote
    return None


def compute_tca_preview(
    pair: str,
    *,
    qty: float | None = None,
    notional: float | None = None,
    horizon_min: float | None = None,
    include_next_window: bool = True,
    rolling_30d_notional: float | Mapping[str, float] | None = None,
    book_liquidity_usdt: float | Mapping[str, float] | None = None,
) -> Dict[str, object]:
    if not feature_enabled():
        raise RuntimeError("FEATURE_TCA_ROUTER disabled")

    state = get_state()
    config = getattr(state.config, "data", None)
    derivatives_cfg = getattr(config, "derivatives", None) if config else None
    if derivatives_cfg is None:
        raise ValueError("derivatives config missing")
    arbitrage_cfg = getattr(derivatives_cfg, "arbitrage", None)
    pairs = getattr(arbitrage_cfg, "pairs", None) if arbitrage_cfg else None
    if not pairs:
        raise ValueError("no arbitrage pairs configured")

    target_norm = normalise_symbol(pair)
    selected = None
    for entry in pairs:
        try:
            long_leg = entry.long
            short_leg = entry.short
            long_symbol_norm = normalise_symbol(long_leg.symbol)
            short_symbol_norm = normalise_symbol(short_leg.symbol)
        except AttributeError as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "tca preview: skipping arbitrage pair entry missing attributes",
                exc_info=exc,
            )
        else:
            if target_norm in {long_symbol_norm, short_symbol_norm}:
                selected = entry
                break
    if selected is None:
        raise ValueError(f"pair {pair} not configured for arbitrage")

    long_leg = selected.long
    short_leg = selected.short
    primary_route = {
        "long": {"venue": str(long_leg.venue), "symbol": str(long_leg.symbol)},
        "short": {"venue": str(short_leg.venue), "symbol": str(short_leg.symbol)},
    }
    reversed_route = {
        "long": {"venue": str(short_leg.venue), "symbol": str(short_leg.symbol)},
        "short": {"venue": str(long_leg.venue), "symbol": str(long_leg.symbol)},
    }
    routes: List[Dict[str, Dict[str, str]]] = [primary_route]
    if (reversed_route["long"]["venue"], reversed_route["short"]["venue"]) != (
        primary_route["long"]["venue"],
        primary_route["short"]["venue"],
    ):
        routes.append(reversed_route)

    venue_alias_map = {
        route_leg["venue"]: route_leg["venue"]
        for route in routes
        for route_leg in (route["long"], route["short"])
    }

    funding_quotes = extract_funding_inputs(
        runtime_state=state,
        symbol=target_norm,
        venue_alias_map=venue_alias_map,
        include_next_window=include_next_window,
    )

    manual_fees = _manual_fee_table(config)
    tier_table = _tier_table_from_config(config)
    impact_model = _impact_model_from_config(config)
    prefer_maker = _prefer_maker(config)
    default_horizon = _default_horizon_minutes(config)

    aggregator = get_market_data()
    symbol_norm = normalise_symbol(pair)
    orderbooks: Dict[str, Dict[str, float]] = {}
    for venue_id in venue_alias_map:
        venue_key = venue_id.replace("_", "-")
        orderbooks[venue_id] = aggregator.top_of_book(venue_key, symbol_norm)

    qty_value = float(qty) if qty is not None else 0.0
    if qty_value <= 0.0:
        reference_notional = (
            float(notional)
            if notional
            else float(getattr(state.control, "order_notional_usdt", 0.0))
        )
        mid_prices = []
        for book in orderbooks.values():
            bid = float(book.get("bid", 0.0))
            ask = float(book.get("ask", 0.0))
            if bid > 0 and ask > 0:
                mid_prices.append((bid + ask) / 2.0)
        reference_price = sum(mid_prices) / len(mid_prices) if mid_prices else 0.0
        if reference_notional > 0 and reference_price > 0:
            qty_value = reference_notional / reference_price
        if qty_value <= 0:
            qty_value = 1.0
    if horizon_min is None:
        horizon_minutes = default_horizon
    else:
        try:
            horizon_minutes = max(float(horizon_min), 0.0)
        except (TypeError, ValueError):
            horizon_minutes = default_horizon
    now_ts = time.time()

    def _rolling_for(venue: str) -> float | None:
        source = rolling_30d_notional
        value: object | None
        if isinstance(source, Mapping):
            value = source.get(venue)
        else:
            value = source
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    evaluated_routes: List[Dict[str, object]] = []
    for route in routes:
        long_meta = route["long"]
        short_meta = route["short"]
        long_venue = long_meta["venue"]
        short_venue = short_meta["venue"]
        long_book = orderbooks.get(long_venue, {})
        short_book = orderbooks.get(short_venue, {})
        long_symbol_raw, long_symbol_norm = _resolve_leg_symbol(
            config, long_venue, long_meta["symbol"], symbol_norm
        )
        short_symbol_raw, short_symbol_norm = _resolve_leg_symbol(
            config, short_venue, short_meta["symbol"], symbol_norm
        )

        long_price = float(long_book.get("ask", 0.0))
        short_price = float(short_book.get("bid", 0.0))
        if long_price <= 0 or short_price <= 0:
            raise ValueError("top of book unavailable for one of the venues")

        long_quote = _coerce_quote(funding_quotes, long_venue)
        short_quote = _coerce_quote(funding_quotes, short_venue)

        long_fee_info: FeeInfo = manual_fees.get(long_venue)
        short_fee_info: FeeInfo = manual_fees.get(short_venue)

        long_taker = long_quote.taker_fee_bps if long_quote else long_fee_info.taker_bps
        long_maker = long_quote.maker_fee_bps if long_quote else long_fee_info.maker_bps
        long_vip = long_quote.vip_rebate_bps if long_quote else long_fee_info.vip_rebate_bps
        long_funding = long_quote.funding_per_hour_bps if long_quote else 0.0
        long_next_minutes = (
            max((long_quote.next_funding_ts - now_ts) / 60.0, 0.0) if long_quote else 0.0
        )
        long_maker_possible = long_quote.maker_possible if long_quote else prefer_maker

        short_taker = short_quote.taker_fee_bps if short_quote else short_fee_info.taker_bps
        short_maker = short_quote.maker_fee_bps if short_quote else short_fee_info.maker_bps
        short_vip = short_quote.vip_rebate_bps if short_quote else short_fee_info.vip_rebate_bps
        short_funding = short_quote.funding_per_hour_bps if short_quote else 0.0
        short_next_minutes = (
            max((short_quote.next_funding_ts - now_ts) / 60.0, 0.0) if short_quote else 0.0
        )
        short_maker_possible = short_quote.maker_possible if short_quote else prefer_maker

        long_cost = effective_cost(
            "long",
            qty=qty_value,
            px=long_price,
            horizon_min=horizon_minutes,
            is_maker_possible=long_maker_possible,
            venue_meta={
                "venue": long_venue,
                "fees": {
                    "maker_bps": long_maker,
                    "taker_bps": long_taker,
                    "vip_rebate_bps": long_vip,
                },
                "funding_bps_per_hour": long_funding,
            },
            tier_table=tier_table,
            rolling_30d_notional=_rolling_for(long_venue),
            impact_model=impact_model,
            book_liquidity_usdt=book_liquidity_usdt,
        )
        short_cost = effective_cost(
            "short",
            qty=qty_value,
            px=short_price,
            horizon_min=horizon_minutes,
            is_maker_possible=short_maker_possible,
            venue_meta={
                "venue": short_venue,
                "fees": {
                    "maker_bps": short_maker,
                    "taker_bps": short_taker,
                    "vip_rebate_bps": short_vip,
                },
                "funding_bps_per_hour": short_funding,
            },
            tier_table=tier_table,
            rolling_30d_notional=_rolling_for(short_venue),
            impact_model=impact_model,
            book_liquidity_usdt=book_liquidity_usdt,
        )

        long_cost["breakdown"].setdefault("funding", {})["next_event_minutes"] = long_next_minutes
        long_cost["breakdown"]["execution"]["maker_possible"] = long_maker_possible
        short_cost["breakdown"].setdefault("funding", {})["next_event_minutes"] = short_next_minutes
        short_cost["breakdown"]["execution"]["maker_possible"] = short_maker_possible

        total_bps = float(long_cost.get("bps", 0.0)) + float(short_cost.get("bps", 0.0))
        total_usdt = float(long_cost.get("usdt", 0.0)) + float(short_cost.get("usdt", 0.0))
        notional_estimate = qty_value * max(long_price, short_price)

        evaluated_routes.append(
            {
                "direction": f"{long_venue}->{short_venue}",
                "long": {
                    "venue": long_venue,
                    "symbol": long_symbol_raw,
                    "symbol_norm": long_symbol_norm,
                    "price": long_price,
                    "cost": long_cost,
                },
                "short": {
                    "venue": short_venue,
                    "symbol": short_symbol_raw,
                    "symbol_norm": short_symbol_norm,
                    "price": short_price,
                    "cost": short_cost,
                },
                "total_bps": total_bps,
                "total_usdt": total_usdt,
                "notional_usdt": notional_estimate,
            }
        )

    best_route = (
        min(evaluated_routes, key=lambda item: item["total_bps"]) if evaluated_routes else None
    )

    LOGGER.debug(
        "tca preview evaluated routes",
        extra={
            "pair": target_norm,
            "qty": qty_value,
            "horizon_min": horizon_minutes,
            "routes": [
                {
                    "direction": route["direction"],
                    "total_bps": round(route["total_bps"], 6),
                    "total_usdt": round(route["total_usdt"], 6),
                    "long_mode": route["long"]["cost"]["breakdown"]["execution"].get("mode"),
                    "short_mode": route["short"]["cost"]["breakdown"]["execution"].get("mode"),
                }
                for route in evaluated_routes
            ],
        },
    )

    return {
        "pair": target_norm,
        "qty": qty_value,
        "horizon_min": horizon_minutes,
        "routes": evaluated_routes,
        "best": best_route,
    }


__all__ = ["compute_tca_preview", "feature_enabled"]
