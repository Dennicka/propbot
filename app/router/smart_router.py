"""Latency and liquidity aware routing decisions."""

from __future__ import annotations

import math
import os
import time
import logging
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Sequence

from ..golden.logger import get_golden_logger
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
            except Exception:  # pragma: no cover - defensive
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
        except Exception:  # pragma: no cover - defensive
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
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.warning(
                            "smart_router: failed to serialise tier entry",
                            extra={"venue": str(venue)},
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


class SmartRouter:
    """Score venues using TCA, liquidity and latency inputs."""

    def __init__(self, *, state=None, market_data=None) -> None:
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

    def _load_liquidity_snapshot(self) -> Dict[str, float]:
        snapshot: Dict[str, float] = {}
        try:
            liquidity_state = get_liquidity_status()
        except Exception:  # pragma: no cover - defensive
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
        except Exception as exc:  # pragma: no cover - defensive guard
            LOGGER.debug("smart_router.tca_failed", extra={"venue": canonical, "error": str(exc)})
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
        except Exception:  # pragma: no cover - fallback
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
        except Exception:  # pragma: no cover - fallback
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


__all__ = ["SmartRouter", "feature_enabled"]
