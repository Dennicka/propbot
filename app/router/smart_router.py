"""Latency and liquidity aware routing decisions."""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, Mapping, Protocol, Sequence

import httpx

import app.config.feature_flags as ff
from app.market.watchdog import watchdog

from ..golden.logger import get_golden_logger
from ..orders.idempotency import IdempoStore, make_coid
from ..orders.quantization import as_dec, quantize_order
from ..audit.counters import AuditCounters
from ..orders.state import OrderState, OrderStateError, next_state, validate_transition
from ..orders.tracker import OrderTracker, TRACKER_MAX_ACTIVE, TRACKER_TTL_SEC
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
from ..risk.limits import RiskGovernor, load_config_from_env

LOGGER = logging.getLogger(__name__)
NANOS_IN_SECOND = 1_000_000_000


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


def _order_tracker_ttl() -> int:
    raw = os.getenv("ORDER_TRACKER_TTL_SEC")
    if raw is None:
        return TRACKER_TTL_SEC
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return TRACKER_TTL_SEC
    return max(value, 0)


def _order_tracker_max_active() -> int:
    raw = os.getenv("ORDER_TRACKER_MAX_ACTIVE")
    if raw is None:
        return TRACKER_MAX_ACTIVE
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return TRACKER_MAX_ACTIVE
    return value if value > 0 else TRACKER_MAX_ACTIVE


class TrackedOrderLike(Protocol):
    """Structural type for tracked order snapshots."""

    order_id: str
    state: OrderState
    last_update_ts: float


@dataclass(slots=True, frozen=True)
class TrackedOrderSnapshot(TrackedOrderLike):
    """Immutable view of a tracked order."""

    order_id: str
    state: OrderState
    last_update_ts: float


@dataclass(slots=True)
class _ScoreResult:
    venue: str
    score: float
    payload: Dict[str, object]


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
        self._order_tracker = OrderTracker(max_active=_order_tracker_max_active())
        self._order_tracker_ttl_sec = _order_tracker_ttl()
        self._completed_orders: Dict[str, tuple[Dict[str, object], int]] = {}
        self._order_strategies: Dict[str, str] = {}
        self._audit_counters = AuditCounters()
        self._last_events: Dict[str, str] = {}
        self._risk_governor: RiskGovernor | None = None
        if ff.risk_limits_on():
            try:
                self._risk_governor = RiskGovernor(load_config_from_env())
            except (ArithmeticError, ValueError) as exc:  # pragma: no cover - defensive
                LOGGER.error(
                    "smart_router.risk_config_failed",
                    extra={
                        "event": "smart_router_risk_config_failed",
                        "component": "smart_router",
                        "details": {},
                    },
                    exc_info=exc,
                )
                self._risk_governor = None

    def _load_symbol_meta(self, venue: str, symbol: str) -> Mapping[str, object]:
        cached = provider.get(venue, symbol)
        if cached is None:
            allowed_raw = os.getenv("TEST_ONLY_ROUTER_META", "").strip()
            if allowed_raw:
                allow_all = allowed_raw.lower() in {"1", "true", "yes", "on", "*", "all"}
                allowed_pairs: set[tuple[str, str]] = set()
                if not allow_all:
                    for chunk in allowed_raw.split(","):
                        entry = chunk.strip()
                        if not entry:
                            continue
                        if ":" not in entry:
                            continue
                        venue_token, symbol_token = entry.split(":", 1)
                        allowed_pairs.add(
                            (venue_token.strip().lower(), symbol_token.strip().upper())
                        )
                venue_key = str(venue).strip().lower()
                symbol_key = str(symbol).strip().upper()
                if allow_all or (venue_key, symbol_key) in allowed_pairs:
                    # if TEST_ONLY:
                    tick_size = Decimal(os.getenv("TEST_ONLY_ROUTER_TICK_SIZE", "0.1"))
                    step_size = Decimal(os.getenv("TEST_ONLY_ROUTER_STEP_SIZE", "0.001"))
                    min_notional_env = os.getenv("TEST_ONLY_ROUTER_MIN_NOTIONAL")
                    min_qty_env = os.getenv("TEST_ONLY_ROUTER_MIN_QTY")
                    min_notional = (
                        Decimal(min_notional_env)
                        if min_notional_env is not None and min_notional_env.strip()
                        else None
                    )
                    min_qty = (
                        Decimal(min_qty_env)
                        if min_qty_env is not None and min_qty_env.strip()
                        else None
                    )
                    symbol_upper = str(symbol).upper()
                    return {
                        "symbol": symbol_upper,
                        "tick_size": tick_size,
                        "step_size": step_size,
                        "min_notional": min_notional,
                        "min_qty": min_qty,
                        "tick": tick_size,
                        "lot": step_size,
                    }
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
    def snapshot_tracked_orders(self) -> Iterable[TrackedOrderLike]:
        """Return a safe snapshot of all tracked orders."""

        tracked_orders = self._order_tracker.snapshot()
        snapshots: list[TrackedOrderSnapshot] = []
        for tracked in tracked_orders:
            last_update_ts = float(tracked.updated_ts)
            if last_update_ts <= 0.0:
                fallback_ns = tracked.updated_ns or tracked.created_ns
                last_update_ts = float(fallback_ns) / NANOS_IN_SECOND
            snapshots.append(
                TrackedOrderSnapshot(
                    order_id=tracked.coid,
                    state=tracked.state,
                    last_update_ts=last_update_ts,
                )
            )
        return tuple(snapshots)

    @property
    def audit_counters(self) -> AuditCounters:
        """Expose audit counters for reconciliation and reporting."""

        return self._audit_counters

    def audit_counters_snapshot(self) -> Dict[str, int]:
        """Return a snapshot of anomaly counters."""

        return self._audit_counters.snapshot()

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
        if self._risk_governor is not None:
            price_for_risk = Decimal("0") if price is None else Decimal(str(price))
            qty_for_risk = Decimal(str(qty))
            ok, reason = self._risk_governor.allow_order(
                venue,
                symbol,
                strategy,
                price_for_risk,
                qty_for_risk,
            )
            if not ok:
                LOGGER.warning(
                    "smart_router.risk_blocked",
                    extra={
                        "event": "smart_router_risk_blocked",
                        "component": "smart_router",
                        "details": {
                            "client_order_id": client_order_id,
                            "venue": venue,
                            "symbol": symbol,
                            "strategy": strategy,
                            "reason": reason,
                        },
                    },
                )
                return {
                    "client_order_id": client_order_id,
                    "status": f"risk-blocked:{reason}",
                    "reason": reason,
                }

        if not self._idempo.should_send(client_order_id):
            tracked = self._order_tracker.get(client_order_id)
            completed = None if tracked is not None else self._completed_orders.get(client_order_id)
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
                response["filled_qty"] = float(tracked.filled)
                response["qty"] = float(tracked.qty)
            elif completed is not None:
                snapshot, _ = completed
                response.update(
                    {
                        "state": snapshot["state"],
                        "filled_qty": snapshot["filled_qty"],
                        "qty": snapshot["qty"],
                    }
                )
            return response

        side_lower = str(side or "").strip().lower()
        price_value = float(price) if price is not None else None
        if ff.pretrade_strict_on():
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

        if ff.md_watchdog_on() and watchdog.is_stale(venue, symbol):
            LOGGER.warning(
                "marketdata-stale: %s/%s",
                venue,
                symbol,
                extra={
                    "event": "smart_router_marketdata_stale",
                    "component": "smart_router",
                    "details": {
                        "client_order_id": client_order_id,
                        "venue": venue,
                        "symbol": symbol,
                        "strategy": strategy,
                    },
                },
            )
            self._idempo.expire(client_order_id)
            return {
                "client_order_id": client_order_id,
                "status": "marketdata_stale",
                "error": "market data stale",
                "reason": "marketdata_stale",
            }

        self._completed_orders.pop(client_order_id, None)
        self._order_strategies[client_order_id] = strategy
        qty_decimal = Decimal(str(qty_value))
        self._order_tracker.register_order(
            client_order_id,
            venue=venue,
            symbol=symbol,
            side=side,
            qty=qty_decimal,
            now_ns=ts_ns,
        )
        try:
            state = self._order_tracker.apply_event(
                client_order_id,
                "submit",
                None,
                ts_ns,
            )
        except ValueError as exc:  # pragma: no cover - defensive
            LOGGER.error(
                "smart_router.invalid_state_transition",
                extra={
                    "event": "smart_router_invalid_state_transition",
                    "component": "smart_router",
                    "details": {
                        "client_order_id": client_order_id,
                        "current_state": OrderState.NEW.value,
                        "event": "submit",
                    },
                },
                exc_info=exc,
            )
            self._idempo.expire(client_order_id)
            self._order_strategies.pop(client_order_id, None)
            raise

        self._last_events[client_order_id] = "submit"

        tracked = self._order_tracker.get(client_order_id)
        response: Dict[str, object] = {
            "client_order_id": client_order_id,
            "state": state,
            "qty": float(tracked.qty) if tracked is not None else qty_value,
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

        tracked = self._order_tracker.get(client_order_id)
        event_key = event.strip().lower()
        if event_key == "expired":
            event_key = "expire"
        if tracked is None:
            if event_key == "ack":
                self._audit_counters.inc("ack_missing_register")
                self._log_audit_anomaly(
                    "ack_missing_register",
                    client_order_id=client_order_id,
                    event=event_key,
                    state=None,
                    details={},
                )
            LOGGER.error(
                "smart_router.unknown_order_event",
                extra={
                    "event": "smart_router_unknown_order_event",
                    "component": "smart_router",
                    "details": {"client_order_id": client_order_id, "event": event_key},
                },
            )
            raise KeyError(f"unknown client order id: {client_order_id}")

        previous_state = tracked.state
        previous_event = self._last_events.get(client_order_id)
        if previous_event == event_key and event_key != "partial_fill":
            self._audit_counters.inc("duplicate_event")
            self._log_audit_anomaly(
                "duplicate_event",
                client_order_id=client_order_id,
                event=event_key,
                state=previous_state,
                details={"previous_event": previous_event},
            )
            return previous_state

        if event_key in {"partial_fill", "filled"} and previous_state not in {
            OrderState.ACK,
            OrderState.PARTIAL,
        }:
            counter = "fill_without_ack" if event_key == "filled" else "out_of_order"
            self._audit_counters.inc(counter)
            self._log_audit_anomaly(
                counter,
                client_order_id=client_order_id,
                event=event_key,
                state=previous_state,
                details={},
            )
            return previous_state

        try:
            candidate_state = next_state(previous_state, event_key)
        except ValueError:
            self._audit_counters.inc("invalid_transition")
            self._log_audit_anomaly(
                "invalid_transition",
                client_order_id=client_order_id,
                event=event_key,
                state=previous_state,
                details={"reason": "unknown_event"},
            )
            return previous_state

        try:
            validate_transition(previous_state, candidate_state)
        except OrderStateError as exc:
            self._audit_counters.inc("invalid_transition")
            self._log_audit_anomaly(
                "invalid_transition",
                client_order_id=client_order_id,
                event=event_key,
                state=previous_state,
                details={"target_state": candidate_state.value, "error": str(exc)},
            )
            return previous_state

        now_ns = time.time_ns()
        now_ts = float(now_ns) / NANOS_IN_SECOND
        qty_value = Decimal(str(quantity)) if quantity is not None else None
        try:
            new_state = self._order_tracker.apply_event(
                client_order_id,
                event_key,
                qty_value,
                now_ns,
            )
        except ValueError as exc:
            self._audit_counters.inc("invalid_transition")
            self._log_audit_anomaly(
                "invalid_transition",
                client_order_id=client_order_id,
                event=event_key,
                state=previous_state,
                details={"reason": "tracker_error"},
            )
            LOGGER.warning(
                "smart_router.invalid_state_transition",
                extra={
                    "event": "smart_router_invalid_state_transition",
                    "component": "smart_router",
                    "details": {
                        "client_order_id": client_order_id,
                        "current_state": previous_state.value,
                        "event": event_key,
                    },
                },
                exc_info=exc,
            )
            return previous_state

        self._last_events[client_order_id] = event_key

        updated = self._order_tracker.get(client_order_id)
        risk_governor = self._risk_governor
        strategy = self._order_strategies.get(client_order_id, "")
        if risk_governor is not None and updated is not None:
            venue = updated.venue
            symbol = updated.symbol
            if event_key == "reject":
                risk_governor.on_reject(venue, symbol, strategy)
            elif event_key == "ack":
                risk_governor.on_ack(venue, symbol, strategy)
            elif event_key == "filled":
                risk_governor.on_filled(venue, symbol, strategy, Decimal("0"))
        if event_key == "ack":
            self._idempo.mark_ack(client_order_id)
        elif event_key == "partial_fill":
            filled_qty = float(updated.filled) if updated is not None else 0.0
            self._idempo.mark_fill(client_order_id, filled_qty)
        elif event_key == "filled":
            filled_qty = float(updated.filled) if updated is not None else 0.0
            self._idempo.mark_fill(client_order_id, filled_qty)
        elif event_key == "canceled":
            self._idempo.mark_cancel(client_order_id)
        elif event_key in {"reject", "expire"}:
            self._idempo.expire(client_order_id)

        if updated is not None and self._order_tracker.is_terminal(new_state):
            snapshot = self._snapshot_from_tracked(updated)
            self._completed_orders[client_order_id] = (snapshot, now_ns)
            self._order_strategies.pop(client_order_id, None)
            self._order_tracker.mark_terminal(client_order_id, new_state, now_ts)
            self._last_events.pop(client_order_id, None)

        self._order_tracker.prune_aged(now_ns, self._order_tracker_ttl_sec)
        self._order_tracker.purge_terminated_older_than(
            self._order_tracker_ttl_sec,
            now_ts,
        )
        self._prune_completed(now_ns)
        if self._order_strategies:
            for coid in list(self._order_strategies.keys()):
                if self._order_tracker.get(coid) is None and coid not in self._completed_orders:
                    self._order_strategies.pop(coid, None)

        return new_state

    def get_order_snapshot(self, client_order_id: str) -> Dict[str, object]:
        tracked = self._order_tracker.get(client_order_id)
        if tracked is not None:
            return self._snapshot_from_tracked(tracked)
        completed = self._completed_orders.get(client_order_id)
        if completed is None:
            raise KeyError(f"unknown client order id: {client_order_id}")
        snapshot, _ = completed
        return snapshot

    @staticmethod
    def _snapshot_from_tracked(tracked) -> Dict[str, object]:
        return {
            "venue": tracked.venue,
            "symbol": tracked.symbol,
            "side": tracked.side,
            "qty": float(tracked.qty),
            "filled_qty": float(tracked.filled),
            "state": tracked.state,
        }

    def _log_audit_anomaly(
        self,
        kind: str,
        *,
        client_order_id: str,
        event: str,
        state: OrderState | None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        payload: Dict[str, object] = {
            "client_order_id": client_order_id,
            "event": event,
            "counters": self._audit_counters.snapshot(),
        }
        if state is not None:
            payload["state"] = state.value
        if details:
            payload.update(details)
        LOGGER.warning(
            "smart_router.audit_%s",  # pragma: no cover - exercised in tests via snapshot
            kind,
            extra={
                "event": f"smart_router_audit_{kind}",
                "component": "smart_router",
                "details": payload,
            },
        )

    def _prune_completed(self, now_ns: int) -> None:
        ttl_sec = self._order_tracker_ttl_sec
        if ttl_sec <= 0 or not self._completed_orders:
            return
        ttl_ns = ttl_sec * NANOS_IN_SECOND
        stale_ids = [
            coid
            for coid, (_, updated_ns) in self._completed_orders.items()
            if now_ns - updated_ns > ttl_ns
        ]
        for coid in stale_ids:
            self._completed_orders.pop(coid, None)

    def purge_terminal_orders(self, *, ttl_sec: int, now_ts: float | None = None) -> int:
        """Purge terminal orders using the provided TTL."""

        effective_now = float(now_ts) if now_ts is not None else time.time()
        removed = self._order_tracker.purge_terminated_older_than(ttl_sec, effective_now)
        return removed

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


__all__ = ["SmartRouter", "feature_enabled"]
