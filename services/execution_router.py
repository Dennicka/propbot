"""Best-venue execution router for hedge legs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, Mapping

import time

from app.router.smart_router import SmartRouter, feature_enabled as smart_router_feature_enabled
from app.router.sor_scoring import (
    RouterVenueCandidate,
    RouterVenueCostEstimate,
    RouterVenueMarketSnapshot,
    RouterVenueScore,
    RouterVenueTradingLimits,
    Side,
    choose_best_venue,
)
from app.services.runtime import (
    get_liquidity_status,
    get_market_data,
    get_state,
    is_dry_run_mode,
)
from app.util.venues import VENUE_ALIASES
from exchanges import BinanceFuturesClient, FuturesExchangeClient, OKXFuturesClient


@dataclass
class _VenueAdapter:
    name: str
    client: FuturesExchangeClient


def _build_clients() -> Dict[str, _VenueAdapter]:
    return {
        "binance": _VenueAdapter("binance", BinanceFuturesClient()),
        "okx": _VenueAdapter("okx", OKXFuturesClient()),
    }


_CLIENTS: Dict[str, _VenueAdapter] = _build_clients()


def _fee_bps_for(venue: str, control: object) -> int:
    venue_lower = venue.lower()
    if venue_lower == "binance":
        return int(getattr(control, "taker_fee_bps_binance", 0))
    if venue_lower == "okx":
        return int(getattr(control, "taker_fee_bps_okx", 0))
    return 0


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _available_balance(entry: Mapping[str, object] | None) -> float | None:
    if not isinstance(entry, Mapping):
        return None
    for key in ("available_balance", "available", "cash_available"):
        if key in entry:
            return _coerce_float(entry.get(key))
    return None


def _decimal_or_none(value: object) -> Decimal | None:
    try:
        if value is None:
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _positive_decimal_or_none(value: object) -> Decimal | None:
    qty = _decimal_or_none(value)
    if qty is None:
        return None
    if qty <= Decimal("0"):
        return None
    return qty


def _fetch_quote(adapter: _VenueAdapter, symbol: str) -> float:
    payload = adapter.client.get_mark_price(symbol)
    if isinstance(payload, Mapping):
        return _coerce_float(payload.get("mark_price"))
    return 0.0


def _check_liquidity(adapter: _VenueAdapter, symbol: str, notional_usdt: float) -> bool:
    if notional_usdt <= 0:
        return False
    if is_dry_run_mode():
        return True
    try:
        limits = adapter.client.get_account_limits()
    except Exception:  # pragma: no cover - network/credential failures fallback
        return True
    available = _available_balance(limits)
    if available is None:
        return True
    try:
        return float(available) >= float(notional_usdt)
    except (TypeError, ValueError):
        return True


def choose_venue(side: str, symbol: str, size: float) -> Dict[str, object]:
    """Return the venue offering the best effective price for the desired leg."""

    side_lower = str(side or "").lower()
    if side_lower not in {"buy", "sell", "long", "short"}:
        raise ValueError("side must be buy/sell or long/short")

    symbol_normalised = str(symbol or "").upper()
    base_size = max(float(size), 0.0)

    state = get_state()
    control = getattr(state, "control", state)

    candidates: Iterable[_VenueAdapter] = _CLIENTS.values()
    market_data = get_market_data()
    liquidity_state = get_liquidity_status()
    liquidity_map: Dict[str, float] = {}
    if isinstance(liquidity_state, Mapping):
        per_venue = liquidity_state.get("per_venue")
        if isinstance(per_venue, Mapping):
            for venue_key, payload in per_venue.items():
                canonical_key = VENUE_ALIASES.get(str(venue_key).lower(), str(venue_key).lower())
                available = _available_balance(payload) if isinstance(payload, Mapping) else None
                if available is not None:
                    liquidity_map[canonical_key] = float(available)

    fallback_best: Dict[str, object] | None = None
    canonical_candidates: Dict[str, Dict[str, object]] = {}
    scoring_candidates: list[RouterVenueCandidate] = []
    rest_latency_map: Dict[str, float] = {}
    ws_latency_map: Dict[str, float] = {}

    for adapter in candidates:
        start_ts = time.time()
        mark_price = _fetch_quote(adapter, symbol_normalised)
        rest_latency_ms = max((time.time() - start_ts) * 1000.0, 0.0)
        fee_bps = _fee_bps_for(adapter.name, control)
        notional_usdt = base_size * mark_price if mark_price > 0 else 0.0
        fee_multiplier = fee_bps / 10_000.0
        if side_lower in {"buy", "long"}:
            effective_price = mark_price * (1.0 + fee_multiplier)
        else:
            effective_price = mark_price * (1.0 - fee_multiplier)
        liquidity_ok = _check_liquidity(adapter, symbol_normalised, notional_usdt)
        canonical = VENUE_ALIASES.get(adapter.name.lower(), adapter.name.lower())
        try:
            book = market_data.top_of_book(canonical, symbol_normalised)
        except Exception:  # pragma: no cover - fallback to mark price only
            book = {}
        book_payload = book if isinstance(book, Mapping) else {}
        ws_latency_ms = 0.0
        ts_value = _coerce_float(book_payload.get("ts"))
        if ts_value > 0:
            ws_latency_ms = max((time.time() - ts_value) * 1000.0, 0.0)
        rest_latency_map[canonical] = rest_latency_ms
        ws_latency_map[canonical] = ws_latency_ms

        best_bid = _decimal_or_none(book_payload.get("bid"))
        if best_bid is None:
            best_bid = _decimal_or_none(book_payload.get("best_bid"))
        best_ask = _decimal_or_none(book_payload.get("ask"))
        if best_ask is None:
            best_ask = _decimal_or_none(book_payload.get("best_ask"))

        best_bid_qty = _positive_decimal_or_none(
            book_payload.get("bid_qty")
            or book_payload.get("best_bid_qty")
            or book_payload.get("bid_size")
        )
        best_ask_qty = _positive_decimal_or_none(
            book_payload.get("ask_qty")
            or book_payload.get("best_ask_qty")
            or book_payload.get("ask_size")
        )

        costs: RouterVenueCostEstimate | None = None
        fee_rate = _decimal_or_none(fee_bps)
        if fee_rate is not None:
            fee_rate = fee_rate / Decimal("10000")
            costs = RouterVenueCostEstimate(fee_rate=fee_rate, funding_rate=None)

        candidate_side: Side = "buy" if side_lower in {"buy", "long"} else "sell"
        quantity_dec = _decimal_or_none(base_size) or Decimal("0")
        notional_dec = _decimal_or_none(notional_usdt) or Decimal("0")
        market_snapshot = RouterVenueMarketSnapshot(
            venue_id=canonical,
            best_bid=best_bid,
            best_ask=best_ask,
            best_bid_qty=best_bid_qty,
            best_ask_qty=best_ask_qty,
        )

        # NOTE: RouterVenueTradingLimits remain None until exchange metadata/configs provide values.
        limits: RouterVenueTradingLimits | None = None
        scoring_candidates.append(
            RouterVenueCandidate(
                venue_id=canonical,
                side=candidate_side,
                quantity=quantity_dec,
                notional_estimate=notional_dec,
                market=market_snapshot,
                costs=costs,
                is_healthy=True,
                risk_allowed=liquidity_ok,
                limits=limits,
            )
        )

        candidate = {
            "venue": adapter.name,
            "canonical_venue": canonical,
            "expected_fill_px": mark_price,
            "fee_bps": fee_bps,
            "effective_price": effective_price,
            "liquidity_ok": liquidity_ok,
            "size": base_size,
            "expected_notional": notional_usdt,
            "rest_latency_ms": rest_latency_ms,
            "ws_latency_ms": ws_latency_ms,
            "book_liquidity_usdt": liquidity_map.get(canonical),
        }
        canonical_candidates[canonical] = candidate
        if fallback_best is None:
            fallback_best = candidate
            continue
        best_eff = _coerce_float(fallback_best.get("effective_price"))
        cand_eff = _coerce_float(candidate.get("effective_price"))
        best_liquidity = bool(fallback_best.get("liquidity_ok"))
        candidate_liquidity = bool(candidate.get("liquidity_ok"))
        if best_liquidity and not candidate_liquidity:
            continue
        if candidate_liquidity and not best_liquidity:
            fallback_best = candidate
            continue
        if side_lower in {"buy", "long"}:
            if cand_eff < best_eff:
                fallback_best = candidate
        else:
            if cand_eff > best_eff:
                fallback_best = candidate

    result = fallback_best or {
        "venue": "binance",
        "canonical_venue": VENUE_ALIASES.get("binance", "binance-um"),
        "expected_fill_px": 0.0,
        "fee_bps": _fee_bps_for("binance", control),
        "effective_price": 0.0,
        "liquidity_ok": False,
        "size": base_size,
        "expected_notional": 0.0,
    }

    best_scoring: RouterVenueScore | None = None
    scoring_info: Dict[str, object] | None = None
    if scoring_candidates:
        best_scoring = choose_best_venue(scoring_candidates)
        if best_scoring is not None and best_scoring.venue_id in canonical_candidates:
            result = dict(canonical_candidates[best_scoring.venue_id])
        if best_scoring is None:
            scoring_info = {"best": None, "score": None, "reason": "no_candidate"}
        else:
            scoring_info = {
                "best": best_scoring.venue_id,
                "score": float(best_scoring.score),
                "reason": best_scoring.reason,
            }
    if scoring_info is None:
        scoring_info = {"best": None, "score": None, "reason": "no_candidates"}

    smart_router_info: Dict[str, object] = {"enabled": False, "scoring": scoring_info}
    if smart_router_feature_enabled():
        smart_router_info = {"enabled": True, "scoring": scoring_info}
        try:
            router = SmartRouter()
            best_venue, scores = router.choose(
                list(canonical_candidates.keys()),
                side=side_lower,
                qty=base_size,
                symbol=symbol_normalised,
                book_liquidity_usdt=liquidity_map,
                rest_latency_ms=rest_latency_map,
                ws_latency_ms=ws_latency_map,
            )
            smart_router_info.update({"best": best_venue, "scores": scores})
            if best_venue and best_venue in canonical_candidates:
                result = dict(canonical_candidates[best_venue])
            elif best_venue and best_venue not in canonical_candidates:
                smart_router_info["fallback_reason"] = "candidate_missing"
        except Exception as exc:  # pragma: no cover - defensive
            smart_router_info["error"] = str(exc)

    result.setdefault("smart_router", smart_router_info)
    if result.get("smart_router") is not smart_router_info:
        result["smart_router"] = smart_router_info

    alias = result.get("venue")
    canonical = result.get("canonical_venue")
    if canonical is None and isinstance(alias, str):
        result["canonical_venue"] = VENUE_ALIASES.get(alias.lower(), alias.lower())

    return result
