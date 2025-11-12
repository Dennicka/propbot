"""Latency and liquidity aware routing decisions."""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Sequence

import httpx

from ..golden.logger import get_golden_logger
from ..orders.idempotency import IdempoStore, make_coid
from ..orders.quantization import as_dec, quantize_order
from ..orders.state import OrderState, next_state
from ..rules.pretrade import PretradeRejection, validate_pretrade
from ..exchanges.metadata import provider
from ..services.runtime import (
    get_liquidity_status,
    get_market_data,
    get_state,
)
from ..tca.cost_model import (
    FeeInfo,
    FeeTable,
    ImpactModel,
    TierTable,
    effective_cost,
)
from ..utils.symbols import normalise_symbol
from ..util.venues import VENUE_ALIASES

LOGGER = logging.getLogger(__name__)


def _maybe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _feature_flag_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def feature_enabled() -> bool:
    """Return True when smart router scoring is enabled."""

    return _feature_flag_enabled("FEATURE_SMART_ROUTER", False)


def ff_pretrade() -> bool:
    import os

    return os.getenv("FF_PRETRADE_STRICT", "0").lower() in {"1", "true", "yes", "on"}


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
            except (AttributeError, TypeError) as exc:  # pragma: no cover - defensive
                LOGGER.debug(
                    "smart_router.manual_fee_serialise_failed",
                    extra={
                        "event": "smart_router_manual_fee_serialise_failed",
                        "module": __name__,
                        "details": {"config": type(manual_cfg).__name__},
                    },
                    exc_info=exc,
                )
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
    mapping: Dict[str, Sequence[Mapping[str, object]]] = {}
    if isinstance(tiers_cfg, Mapping):
        items = tiers_cfg.items()
    else:
        try:
            items = tiers_cfg.model_dump().items()  # type: ignore[attr-defined]
        except (AttributeError, TypeError) as exc:  # pragma: no cover - defensive
            LOGGER.debug(
                "smart_router.tier_table_serialise_failed",
                extra={
                    "event": "smart_router_tier_table_serialise_failed",
                    "module": __name__,
                    "details": {"config": type(tiers_cfg).__name__},
                },
                exc_info=exc,
            )
            items = []
    for venue, payload in items:
        tier_entries: list[Mapping[str, object]] = []
        if isinstance(payload, Iterable):
            for entry in payload:
                if isinstance(entry, Mapping):
                    tier_entries.append(entry)
                else:
                    try:
                        tier_entries.append(entry.model_dump())  # type: ignore[attr-defined]
                    except (AttributeError, TypeError) as exc:  # noqa: BLE001
                        LOGGER.warning(
                            "smart_router.tier_entry_serialise_failed",
                            extra={
                                "event": "smart_router_tier_entry_serialise_failed",
                                "module": __name__,
                                "details": {"venue": str(venue)},
                            },
                            exc_info=exc,
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
    return horizon_float if horizon_float >= 0 else 0.0


def _prefer_maker(config) -> bool:
    derivatives_cfg = getattr(config, "derivatives", None)
    arbitrage_cfg = getattr(derivatives_cfg, "arbitrage", None) if derivatives_cfg else None
    return bool(getattr(arbitrage_cfg, "prefer_maker", False))


def _latency_target_ms(config) -> float:
    derivatives_cfg = getattr(config, "derivatives", None)
    arbitrage_cfg = getattr(derivatives_cfg, "arbitrage", None) if derivatives_cfg else None
    target_value = getattr(arbitrage_cfg, "max_latency_ms", 200) if arbitrage_cfg else 200
    try:
        return float(target_value)
    except (TypeError, ValueError):
        return 200.0


def _latency_weight_bps_per_ms() -> float:
    raw = os.getenv("SMART_ROUTER_LATENCY_BPS_PER_MS")
    if raw is None:
        return 0.01
    try:
        weight = float(raw)
    except (TypeError, ValueError):
        return 0.01
    return max(weight, 0.0)


@dataclass(slots=True)
class _ScoreResult:
    venue: str
    score: float
    payload: Dict[str, object]


@dataclass(slots=True)
class _TrackedOrder:
    strategy: str
    venue: str
    symbol: str
    side: str
    qty: float
    state: OrderState = OrderState.NEW
    filled_qty: float = 0.0

    def snapshot(self) -> Dict[str, object]:
        return {
            "strategy": self.strategy,
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "filled_qty": self.filled_qty,
            "state": self.state,
        }


class SmartRouter:
    """Score venues using TCA, liquidity and latency inputs."""

    def __init__(
        self,
        *,
        state=None,
        market_data=None,
        idempo_store: IdempoStore | None = None,
    ) -> None:
        self._state = state if state is not None else get_state()
        config = getattr(self._state, "config", None)
        self._config = getattr(config, "data", None)
        self._market_data = market_data if market_data is not None else get_market_data()
        self._manual_fees = _manual_fee_table(self._config) if self._config else FeeTable()
        self._tier_table = _tier_table_from_config(self._config) if self._config else None
        self._impact_model = _impact_model_from_config(self._config) if self._config else None
        self._horizon_min = _default_horizon_minutes(self._config) if self._config else 60.0
        self._prefer_maker = _prefer_maker(self._config) if self._config else False
        self._latency_target_ms = _latency_target_ms(self._config) if self._config else 200.0
        self._latency_weight = _latency_weight_bps_per_ms()
        self._liquidity_snapshot = self._load_liquidity_snapshot()
        self._idempo = idempo_store if idempo_store is not None else IdempoStore()
        self._orders: Dict[str, _TrackedOrder] = {}

    def _load_symbol_meta(self, venue: str, symbol: str) -> Mapping[str, object]:
        cached = provider.get(venue, symbol)
        if cached is None:
            LOGGER.warning(
                "smart_router.meta_missing",
                extra={
                    "event": "smart_router_meta_missing",
                    "component": "smart_router",
                    "details": {"venue": venue, "symbol": symbol},
                },
            )
            raise PretradeRejection("no_meta")
        symbol_upper = str(symbol).upper()
        payload: dict[str, object] = {
            "symbol": symbol_upper,
            "tick_size": cached.tick_size,
            "step_size": cached.step_size,
            "min_notional": cached.min_notional,
            "min_qty": cached.min_qty,
            "tick": cached.tick_size,
            "lot": cached.step_size,
        }
        return payload

    # ------------------------------------------------------------------
    # Order lifecycle helpers
    # ------------------------------------------------------------------
    def register_order(
        self,
        *,
        strategy: str,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: float | None = None,
        ts_ns: int,
        nonce: int,
    ) -> Dict[str, object]:
        """Register an outbound order intent and enforce idempotency."""

        client_order_id = make_coid(strategy, venue, symbol, side, ts_ns, nonce)
        if not self._idempo.should_send(client_order_id):
            tracked = self._orders.get(client_order_id)
            LOGGER.warning(
                "smart_router.idempotent_skip",
                extra={
                    "event": "smart_router_idempotent_skip",
                    "component": "smart_router",
                    "details": {
                        "client_order_id": client_order_id,
                        "venue": venue,
                        "symbol": symbol,
                        "strategy": strategy,
                    },
                },
            )
            response: Dict[str, object] = {
                "client_order_id": client_order_id,
                "status": "idempotent_skip",
            }
            if tracked is not None:
                response["state"] = tracked.state
                response["filled_qty"] = tracked.filled_qty
                response["qty"] = tracked.qty
            return response

        side_lower = str(side or "").strip().lower()
        price_value = float(price) if price is not None else None
        if ff_pretrade():
            try:
                meta = self._load_symbol_meta(venue, symbol)
                price_dec = as_dec(price, field="price", allow_none=True)
                qty_dec = as_dec(qty, field="qty")
                q_price, q_qty = quantize_order(side_lower, price_dec, qty_dec, meta)
                validate_pretrade(side_lower, q_price, q_qty, meta)
            except PretradeRejection as exc:
                LOGGER.warning(
                    "smart_router.pretrade_rejected",
                    extra={
                        "event": "smart_router_pretrade_rejected",
                        "component": "smart_router",
                        "details": {
                            "client_order_id": client_order_id,
                            "venue": venue,
                            "symbol": symbol,
                            "strategy": strategy,
                            "side": side_lower,
                            "reason": exc.reason,
                        },
                    },
                )
                self._idempo.expire(client_order_id)
                return {
                    "client_order_id": client_order_id,
                    "status": "pretrade_rejected",
                    "error": "pretrade rejected",
                    "reason": exc.reason,
                }
            qty_value = float(q_qty)
            price_value = float(q_price) if q_price is not None else price_value
        else:
            qty_value = max(float(qty), 0.0)

        tracked = self._orders.get(client_order_id)
        if tracked is None:
            tracked = _TrackedOrder(
                strategy=strategy,
                venue=venue,
                symbol=symbol,
                side=side,
                qty=qty_value,
            )
            self._orders[client_order_id] = tracked
        else:
            tracked.qty = qty_value

        try:
            tracked.state = next_state(tracked.state, "submit")
        except ValueError as exc:  # pragma: no cover - defensive
            LOGGER.error(
                "smart_router.invalid_state_transition",
                extra={
                    "event": "smart_router_invalid_state_transition",
                    "component": "smart_router",
                    "details": {
                        "client_order_id": client_order_id,
                        "current_state": tracked.state.value,
                        "event": "submit",
                    },
                },
                exc_info=exc,
            )
            self._idempo.expire(client_order_id)
            raise

        tracked.filled_qty = min(tracked.filled_qty, tracked.qty)
        response: Dict[str, object] = {
            "client_order_id": client_order_id,
            "state": tracked.state,
            "qty": tracked.qty,
        }
        if price_value is not None:
            response["price"] = price_value
        return response

    def process_order_event(
        self,
        *,
        client_order_id: str,
        event: str,
        quantity: float | None = None,
    ) -> OrderState:
        """Apply an order lifecycle event and update idempotency state."""

        tracked = self._orders.get(client_order_id)
        if tracked is None:
            LOGGER.error(
                "smart_router.unknown_order_event",
                extra={
                    "event": "smart_router_unknown_order_event",
                    "component": "smart_router",
                    "details": {"client_order_id": client_order_id, "event": event},
                },
            )
            raise KeyError(f"unknown client order id: {client_order_id}")

        event_key = event.strip().lower()
        if event_key == "expired":
            event_key = "expire"
        try:
            new_state = next_state(tracked.state, event_key)
        except ValueError as exc:
            LOGGER.error(
                "smart_router.invalid_state_transition",
                extra={
                    "event": "smart_router_invalid_state_transition",
                    "component": "smart_router",
                    "details": {
                        "client_order_id": client_order_id,
                        "current_state": tracked.state.value,
                        "event": event_key,
                    },
                },
                exc_info=exc,
            )
            raise

        if event_key == "ack":
            self._idempo.mark_ack(client_order_id)
        elif event_key == "partial_fill":
            increment = self._coerce_positive(quantity)
            tracked.filled_qty = min(tracked.qty, tracked.filled_qty + increment)
            self._idempo.mark_fill(client_order_id, tracked.filled_qty)
        elif event_key == "filled":
            increment = self._coerce_positive(quantity)
            if increment <= 0.0:
                increment = max(tracked.qty - tracked.filled_qty, 0.0)
            tracked.filled_qty = min(tracked.qty, tracked.filled_qty + increment)
            self._idempo.mark_fill(client_order_id, tracked.filled_qty)
        elif event_key == "canceled":
            self._idempo.mark_cancel(client_order_id)
        elif event_key in {"reject", "expired", "expire"}:
            self._idempo.expire(client_order_id)

        tracked.state = new_state
        if new_state == OrderState.FILLED and tracked.qty > 0:
            tracked.filled_qty = tracked.qty
        return tracked.state

    def get_order_snapshot(self, client_order_id: str) -> Dict[str, object]:
        tracked = self._orders.get(client_order_id)
        if tracked is None:
            raise KeyError(f"unknown client order id: {client_order_id}")
        return tracked.snapshot()

    def _load_liquidity_snapshot(self) -> Dict[str, float]:
        snapshot: Dict[str, float] = {}
        try:
            liquidity_state = get_liquidity_status()
        except (RuntimeError, ValueError, TypeError) as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "smart_router.liquidity_snapshot_failed",
                extra={
                    "event": "smart_router_liquidity_snapshot_failed",
                    "module": __name__,
                    "details": {},
                },
                exc_info=exc,
            )
            return snapshot
        per_venue = liquidity_state.get("per_venue") if isinstance(liquidity_state, Mapping) else {}
        if not isinstance(per_venue, Mapping):
            return snapshot
        for venue, payload in per_venue.items():
            canonical = VENUE_ALIASES.get(str(venue).lower(), str(venue).lower())
            if isinstance(payload, Mapping):
                for key in ("available_balance", "available", "cash_available"):
                    if key in payload:
                        value = _maybe_float(payload.get(key))
                        if value is not None:
                            snapshot[canonical] = value
                        else:  # pragma: no cover - defensive
                            LOGGER.debug(
                                "failed to parse liquidity balance",
                                extra={"venue": canonical, "key": key},
                            )
                        break
        return snapshot

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    def available_venues(self) -> Sequence[str]:
        venues: list[str] = []
        derivatives = getattr(self._state, "derivatives", None)
        runtime_venues = getattr(derivatives, "venues", {}) if derivatives else {}
        for venue_id in runtime_venues.keys():
            canonical = VENUE_ALIASES.get(str(venue_id).lower(), str(venue_id).lower())
            if canonical not in venues:
                venues.append(canonical)
        if not venues:
            venues = sorted(
                {
                    VENUE_ALIASES.get("binance", "binance-um"),
                    VENUE_ALIASES.get("okx", "okx-perp"),
                }
            )
        return venues

    def score(
        self,
        venue: str,
        *,
        side: str,
        qty: float,
        symbol: str,
        book_liq_usdt: float | None,
        rest_latency_ms: float | None,
        ws_latency_ms: float | None,
    ) -> Dict[str, object]:
        """Return a detailed score for the given venue."""

        side_lower = str(side or "").strip().lower()
        if side_lower not in {"buy", "sell", "long", "short"}:
            raise ValueError("side must be buy/sell or long/short")
        canonical = VENUE_ALIASES.get(str(venue).lower(), str(venue).lower())
        qty_value = max(float(qty), 0.0)
        symbol_norm = normalise_symbol(symbol)

        price = self._resolve_price(canonical, symbol_norm, side_lower)
        if price <= 0.0 or qty_value <= 0.0:
            return {
                "venue": canonical,
                "score": math.inf,
                "error": "price_or_qty_invalid",
            }

        notional = qty_value * price
        liquidity_value = self._resolve_liquidity(canonical, book_liq_usdt, notional)
        rest_latency_value = self._coerce_float(rest_latency_ms)
        ws_latency_value = self._resolve_ws_latency(canonical, symbol_norm, ws_latency_ms)

        fee_info = self._resolve_fee_info(canonical)
        venue_meta = {
            "venue": canonical,
            "fees": {
                "maker_bps": fee_info.maker_bps,
                "taker_bps": fee_info.taker_bps,
                "vip_rebate_bps": fee_info.vip_rebate_bps,
            },
        }
        maker_possible = bool(
            self._prefer_maker or getattr(self._state.control, "post_only", False)
        )

        try:
            cost_payload = effective_cost(
                side_lower,
                qty=qty_value,
                px=price,
                horizon_min=self._horizon_min,
                is_maker_possible=maker_possible,
                venue_meta=venue_meta,
                tier_table=self._tier_table,
                rolling_30d_notional=None,
                impact_model=self._impact_model,
                book_liquidity_usdt=liquidity_value,
            )
        except (ValueError, TypeError, RuntimeError) as exc:  # pragma: no cover - defensive guard
            LOGGER.warning(
                "smart_router.tca_failed",
                extra={
                    "event": "smart_router_tca_failed",
                    "module": __name__,
                    "details": {"venue": canonical, "symbol": symbol_norm},
                },
                exc_info=exc,
            )
            return {
                "venue": canonical,
                "score": math.inf,
                "error": "tca_failed",
            }

        base_cost_usdt = float(cost_payload.get("usdt", 0.0))
        breakdown = cost_payload.get("breakdown") if isinstance(cost_payload, Mapping) else {}
        impact_bps = 0.0
        impact_usdt_included = 0.0
        if isinstance(breakdown, Mapping):
            impact = breakdown.get("impact")
            if isinstance(impact, Mapping):
                try:
                    impact_bps = float(impact.get("bps", 0.0))
                except (TypeError, ValueError):
                    impact_bps = 0.0
                try:
                    impact_usdt_included = float(impact.get("usdt", 0.0))
                except (TypeError, ValueError):
                    impact_usdt_included = 0.0
        impact_target_usdt = max(notional * impact_bps / 10_000.0, 0.0)
        impact_penalty_usdt = max(impact_target_usdt - impact_usdt_included, 0.0)

        latency_penalty_usdt, latency_bps = self._latency_penalty(
            rest_latency_value,
            ws_latency_value,
            notional,
        )

        total_cost = base_cost_usdt + impact_penalty_usdt + latency_penalty_usdt
        result = {
            "venue": canonical,
            "score": total_cost,
            "price": price,
            "qty": qty_value,
            "notional": notional,
            "base_cost_usdt": base_cost_usdt,
            "impact_penalty_usdt": impact_penalty_usdt,
            "latency_penalty_usdt": latency_penalty_usdt,
            "latency_bps": latency_bps,
            "rest_latency_ms": rest_latency_value,
            "ws_latency_ms": ws_latency_value,
            "book_liquidity_usdt": liquidity_value,
            "tca": cost_payload,
        }
        return result

    def choose(
        self,
        venues: Sequence[str],
        *,
        side: str,
        qty: float,
        symbol: str,
        book_liquidity_usdt: Mapping[str, float] | None = None,
        rest_latency_ms: Mapping[str, float] | None = None,
        ws_latency_ms: Mapping[str, float] | None = None,
    ) -> tuple[str | None, Dict[str, Dict[str, object]]]:
        """Return the best venue and the per-venue scores."""

        if not venues:
            return None, {}
        scores: Dict[str, Dict[str, object]] = {}
        best: _ScoreResult | None = None
        for venue in venues:
            canonical = VENUE_ALIASES.get(str(venue).lower(), str(venue).lower())
            result = self.score(
                canonical,
                side=side,
                qty=qty,
                symbol=symbol,
                book_liq_usdt=(
                    (book_liquidity_usdt or {}).get(canonical) if book_liquidity_usdt else None
                ),
                rest_latency_ms=(rest_latency_ms or {}).get(canonical) if rest_latency_ms else None,
                ws_latency_ms=(ws_latency_ms or {}).get(canonical) if ws_latency_ms else None,
            )
            scores[canonical] = result
            score_value = float(result.get("score", math.inf))
            if best is None or score_value < best.score:
                best = _ScoreResult(canonical, score_value, result)
            elif best is not None and math.isclose(
                score_value, best.score, rel_tol=1e-9, abs_tol=1e-9
            ):
                if canonical < best.venue:
                    best = _ScoreResult(canonical, score_value, result)
        logger = get_golden_logger()
        if logger.enabled:
            summary: Dict[str, Dict[str, object]] = {}
            for venue_key, payload in scores.items():
                summary_payload: Dict[str, object] = {}
                for key in ("score", "price", "qty", "notional", "latency_bps", "error"):
                    if key in payload and payload.get(key) is not None:
                        value = payload.get(key)
                        if isinstance(value, (int, float)):
                            summary_payload[key] = float(value)
                        else:
                            try:
                                summary_payload[key] = float(value)  # type: ignore[arg-type]
                            except (TypeError, ValueError):
                                summary_payload[key] = value
                summary[venue_key] = summary_payload
            logger.log(
                "route_decision",
                {
                    "symbol": symbol,
                    "side": str(side),
                    "qty": float(qty) if isinstance(qty, (int, float)) else qty,
                    "best": best.venue if best else None,
                    "scores": summary,
                },
            )
        return (best.venue if best else None, scores)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve_price(self, venue: str, symbol: str, side: str) -> float:
        try:
            book = self._market_data.top_of_book(venue, symbol)
        except (
            KeyError,
            LookupError,
            RuntimeError,
            ValueError,
            OSError,
            httpx.HTTPError,
        ) as exc:  # pragma: no cover - fallback
            LOGGER.debug(
                "smart_router.price_lookup_failed",
                extra={
                    "event": "smart_router_price_lookup_failed",
                    "module": __name__,
                    "details": {"venue": venue, "symbol": symbol},
                },
                exc_info=exc,
            )
            return 0.0
        bid = self._coerce_float(book.get("bid"))
        ask = self._coerce_float(book.get("ask"))
        if side in {"buy", "long"}:
            if ask > 0:
                return ask
            if bid > 0:
                return bid
        else:
            if bid > 0:
                return bid
            if ask > 0:
                return ask
        midpoint = 0.0
        if bid > 0 and ask > 0:
            midpoint = (bid + ask) / 2.0
        return midpoint

    def _resolve_liquidity(self, venue: str, provided: float | None, notional: float) -> float:
        if provided is not None:
            try:
                value = float(provided)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                return value
        snapshot_value = self._liquidity_snapshot.get(venue)
        if snapshot_value is not None and snapshot_value > 0:
            return float(snapshot_value)
        return max(notional * 2.0, 0.0)

    def _resolve_ws_latency(self, venue: str, symbol: str, provided: float | None) -> float:
        if provided is not None:
            try:
                return float(provided)
            except (TypeError, ValueError):
                return 0.0
        try:
            book = self._market_data.top_of_book(venue, symbol)
        except (
            KeyError,
            LookupError,
            RuntimeError,
            ValueError,
            OSError,
            httpx.HTTPError,
        ) as exc:  # pragma: no cover - fallback
            LOGGER.debug(
                "smart_router.ws_latency_lookup_failed",
                extra={
                    "event": "smart_router_ws_latency_lookup_failed",
                    "module": __name__,
                    "details": {"venue": venue, "symbol": symbol},
                },
                exc_info=exc,
            )
            return 0.0
        ts_value = self._coerce_float(book.get("ts"))
        if ts_value <= 0:
            return 0.0
        now = time.time()
        latency_ms = max((now - ts_value) * 1000.0, 0.0)
        return latency_ms

    def _resolve_fee_info(self, venue: str) -> FeeInfo:
        info = self._manual_fees.get(venue)
        control = getattr(self._state, "control", None)
        if info.maker_bps == 0.0 and info.taker_bps == 0.0 and control is not None:
            if venue == VENUE_ALIASES.get("binance", "binance-um"):
                taker = getattr(control, "taker_fee_bps_binance", 0)
            elif venue == VENUE_ALIASES.get("okx", "okx-perp"):
                taker = getattr(control, "taker_fee_bps_okx", 0)
            else:
                taker = getattr(control, "default_taker_fee_bps", 0)
            info = FeeInfo(maker_bps=float(taker), taker_bps=float(taker), vip_rebate_bps=0.0)
        return info

    def _latency_penalty(
        self, rest_ms: float, ws_ms: float, notional: float
    ) -> tuple[float, float]:
        rest_value = max(rest_ms, 0.0)
        ws_value = max(ws_ms, 0.0)
        over_rest = max(rest_value - self._latency_target_ms, 0.0)
        over_ws = max(ws_value - self._latency_target_ms, 0.0)
        latency_bps = self._latency_weight * (over_rest + over_ws)
        penalty_usdt = max(notional * latency_bps / 10_000.0, 0.0)
        return penalty_usdt, latency_bps

    @staticmethod
    def _coerce_float(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _coerce_positive(value: float | None) -> float:
        if value is None:
            return 0.0
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(numeric, 0.0)


__all__ = ["SmartRouter", "feature_enabled"]
