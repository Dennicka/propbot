from __future__ import annotations

"""Sliding-window risk governor with throttling and auto-hold escalation."""

import math
import os
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Mapping

from ..metrics import (
    increment_risk_window,
    record_blocked_order,
    record_risk_check,
    set_risk_error_rate,
    set_risk_success_rate,
    set_risk_throttled,
    set_velocity,
)
from ..services import runtime
from ..watchdog.core import (
    BrokerStateSnapshot,
    STATE_DEGRADED,
    STATE_DOWN,
    STATE_UP,
    get_broker_state,
)

_ORDER_OK = "ok"
_ORDER_ERROR = "error"

_STATE_ORDER = {STATE_UP: 2, STATE_DEGRADED: 1, STATE_DOWN: 0}
_DEFAULT_WINDOW_SEC = 3600.0
_DEFAULT_MIN_SUCCESS = 0.985
_DEFAULT_MAX_ERROR = 0.01
_DEFAULT_MIN_STATE = STATE_UP
_DEFAULT_HOLD_AFTER = 2


_INF = float("inf")


def _positive_float(value: float | int | None) -> float | None:
    try:
        numeric = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if numeric is None or numeric <= 0:
        return None
    return numeric


def _positive_int(value: int | float | None) -> int | None:
    try:
        numeric = int(float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None
    if numeric is None or numeric <= 0:
        return None
    return numeric


def _normalise_strategy(name: str | None) -> str | None:
    if not name:
        return None
    value = str(name).strip().lower()
    return value or None


def _normalise_symbol(name: str | None) -> str | None:
    if not name:
        return None
    value = str(name).strip().upper()
    return value or None


@dataclass(slots=True)
class RiskCaps:
    """Container describing the configured caps enforced pre-trade."""

    global_notional: float | None = None
    per_strategy_notional: Dict[str, float] = field(default_factory=dict)
    per_symbol_notional: Dict[str, float] = field(default_factory=dict)
    max_open_positions_global: int | None = None
    per_strategy_positions: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.global_notional = _positive_float(self.global_notional)
        self.max_open_positions_global = _positive_int(self.max_open_positions_global)

        strategy_notional: Dict[str, float] = {}
        for raw, value in dict(self.per_strategy_notional).items():
            limit = _positive_float(value)
            key = _normalise_strategy(raw)
            if key and limit is not None:
                strategy_notional[key] = limit
        self.per_strategy_notional = strategy_notional

        symbol_notional: Dict[str, float] = {}
        for raw, value in dict(self.per_symbol_notional).items():
            limit = _positive_float(value)
            key = _normalise_symbol(raw)
            if key and limit is not None:
                symbol_notional[key] = limit
        self.per_symbol_notional = symbol_notional

        strategy_positions: Dict[str, int] = {}
        for raw, value in dict(self.per_strategy_positions).items():
            limit = _positive_int(value)
            key = _normalise_strategy(raw)
            if key and limit is not None:
                strategy_positions[key] = limit
        self.per_strategy_positions = strategy_positions

    def strategy_notional_limit(self, strategy: str | None) -> float | None:
        if not self.per_strategy_notional:
            return None
        key = _normalise_strategy(strategy)
        if key is None:
            return self.per_strategy_notional.get("__default__")
        return self.per_strategy_notional.get(key) or self.per_strategy_notional.get("__default__")

    def symbol_notional_limit(self, symbol: str | None) -> float | None:
        if not self.per_symbol_notional:
            return None
        key = _normalise_symbol(symbol)
        if key is None:
            return self.per_symbol_notional.get("__default__")
        return self.per_symbol_notional.get(key) or self.per_symbol_notional.get("__default__")

    def strategy_position_limit(self, strategy: str | None) -> int | None:
        if not self.per_strategy_positions:
            return None
        key = _normalise_strategy(strategy)
        if key is None:
            return self.per_strategy_positions.get("__default__")
        return self.per_strategy_positions.get(key) or self.per_strategy_positions.get("__default__")


class VelocityWindow:
    """Track order placement velocity within a sliding time window."""

    def __init__(self, *, bucket_s: int = 60, clock: Callable[[], float] | None = None) -> None:
        self.bucket_s = max(int(bucket_s or 60), 1)
        self._clock = clock or time.time
        self._events: Deque[tuple[float, int, int, float]] = deque()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def _prune_locked(self, now: float) -> None:
        threshold = now - self.bucket_s
        while self._events and self._events[0][0] < threshold:
            self._events.popleft()

    def _aggregate_locked(self) -> tuple[int, int, float]:
        orders = 0
        cancels = 0
        notional = 0.0
        for _, o_count, c_count, notional_value in self._events:
            orders += o_count
            cancels += c_count
            notional += notional_value
        return orders, cancels, notional

    # ------------------------------------------------------------------
    def record(self, *, orders: int = 0, cancels: int = 0, notional: float = 0.0) -> tuple[int, int, float]:
        now = self._clock()
        entry = (
            now,
            max(int(orders), 0),
            max(int(cancels), 0),
            max(float(notional), 0.0),
        )
        with self._lock:
            self._events.append(entry)
            self._prune_locked(now)
            return self._aggregate_locked()

    def totals(self) -> tuple[int, int, float]:
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            return self._aggregate_locked()

    def bucket_for(self, timestamp: float | None = None) -> float:
        if timestamp is None:
            timestamp = self._clock()
        bucket = math.floor(timestamp / self.bucket_s) * self.bucket_s
        return float(bucket)


class RiskGovernor:
    """Pre-trade risk governor enforcing caps and velocity limits."""

    def __init__(
        self,
        caps: RiskCaps,
        *,
        velocity_limits: Mapping[str, float | int | None] | None = None,
        bucket_s: int = 60,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._caps = caps
        self._velocity_limits = {
            "orders": _positive_float((velocity_limits or {}).get("orders")) or _INF,
            "cancels": _positive_float((velocity_limits or {}).get("cancels")) or _INF,
            "notional": _positive_float((velocity_limits or {}).get("notional")) or _INF,
        }
        self._clock = clock or time.time
        self._velocity = VelocityWindow(bucket_s=bucket_s, clock=self._clock)
        self._lock = threading.RLock()
        self.throttled: bool = False
        self.last_reason: str | None = None
        self.last_violation: Dict[str, object] = {}
        self._current_bucket: float | None = None
        self._bucket_violation: bool = False
        self._completed_buckets: Deque[bool] = deque(maxlen=4)

    # ------------------------------------------------------------------
    def check_and_account(
        self,
        ctx: Mapping[str, object] | None,
        order_req: Mapping[str, object] | None,
    ) -> tuple[bool, str | None]:
        """Evaluate risk caps/velocity for the supplied order request."""

        order_payload = dict(order_req or {})
        operation = str(order_payload.get("operation") or "order").strip().lower() or "order"
        strategy = _normalise_strategy(order_payload.get("strategy"))
        symbol = _normalise_symbol(order_payload.get("symbol"))
        try:
            notional = max(float(order_payload.get("notional", 0.0) or 0.0), 0.0)
        except (TypeError, ValueError):
            notional = 0.0
        try:
            positions_delta = int(float(order_payload.get("positions_delta", 0) or 0))
        except (TypeError, ValueError):
            positions_delta = 0
        if positions_delta < 0:
            positions_delta = 0

        context = self._build_context(ctx)
        violation_reason: str | None = None
        violation_details: Dict[str, object] = {}

        with self._lock:
            now = self._clock()
            self._rotate_bucket(now)

            reason, details = self._check_caps(context, strategy, symbol, notional, positions_delta)
            if reason is None:
                reason, details = self._check_velocity(operation, notional)

            if reason is None:
                self._record_success(operation, notional)
                record_risk_check("allow", None)
                self.reset_throttle_if_ok()
                return True, None

            violation_reason = reason
            violation_details = details
            self._record_violation(reason, details)
            record_risk_check("block", reason)
            record_blocked_order(reason)
            return False, violation_reason

    # ------------------------------------------------------------------
    def reset_throttle_if_ok(self) -> None:
        """Clear the throttle after two consecutive violation-free windows."""

        with self._lock:
            if not self.throttled:
                return
            tail = list(self._completed_buckets)[-2:]
            if len(tail) < 2:
                return
            if any(tail):
                return
            self.throttled = False
            self.last_reason = None
            self.last_violation = {}
            set_risk_throttled(False, None)

    def kill_switch(self, reason: str = "KILL_SWITCH::MANUAL") -> None:
        """Engage the kill switch and trip the throttle."""

        with self._lock:
            self.throttled = True
            self.last_reason = reason
            self.last_violation = {"kill_switch": reason}
            self._bucket_violation = True
        set_risk_throttled(True, reason)
        try:
            runtime.update_risk_throttle(True, reason=reason, source="risk_governor_v2")
        except Exception:  # pragma: no cover - defensive
            pass

    # ------------------------------------------------------------------
    def _build_context(self, ctx: Mapping[str, object] | None) -> Dict[str, object]:
        context: Dict[str, object] = {
            "global_notional": 0.0,
            "global_positions": 0,
            "per_strategy_notional": {},
            "per_strategy_positions": {},
            "per_symbol_notional": {},
        }
        if isinstance(ctx, Mapping):
            try:
                context.update({
                    "global_notional": float(ctx.get("global_notional", 0.0) or 0.0),
                    "global_positions": int(float(ctx.get("global_positions", 0) or 0)),
                })
            except (TypeError, ValueError):
                pass
            per_strategy_notional = ctx.get("per_strategy_notional")
            if isinstance(per_strategy_notional, Mapping):
                context["per_strategy_notional"] = {
                    key: max(float(value or 0.0), 0.0)
                    for key, value in per_strategy_notional.items()
                }
            per_strategy_positions = ctx.get("per_strategy_positions")
            if isinstance(per_strategy_positions, Mapping):
                context["per_strategy_positions"] = {
                    key: max(int(float(value or 0)), 0)
                    for key, value in per_strategy_positions.items()
                }
            per_symbol_notional = ctx.get("per_symbol_notional")
            if isinstance(per_symbol_notional, Mapping):
                context["per_symbol_notional"] = {
                    _normalise_symbol(key) or key: max(float(value or 0.0), 0.0)
                    for key, value in per_symbol_notional.items()
                }

        if not context["per_strategy_notional"] or not context["per_strategy_positions"]:
            from . import accounting  # local import to avoid cycle

            snapshot = accounting.get_risk_snapshot()
            totals = snapshot.get("totals") if isinstance(snapshot, Mapping) else {}
            if isinstance(totals, Mapping):
                try:
                    context["global_notional"] = float(totals.get("open_notional", context["global_notional"]))
                except (TypeError, ValueError):
                    pass
                try:
                    context["global_positions"] = int(
                        float(totals.get("open_positions", context["global_positions"]))
                    )
                except (TypeError, ValueError):
                    pass
            per_strategy = snapshot.get("per_strategy") if isinstance(snapshot, Mapping) else {}
            if isinstance(per_strategy, Mapping):
                strategy_notional: Dict[str, float] = {}
                strategy_positions: Dict[str, int] = {}
                for name, payload in per_strategy.items():
                    if not isinstance(payload, Mapping):
                        continue
                    key = _normalise_strategy(name)
                    if key is None:
                        continue
                    try:
                        strategy_notional[key] = max(float(payload.get("open_notional", 0.0) or 0.0), 0.0)
                    except (TypeError, ValueError):
                        continue
                    try:
                        strategy_positions[key] = max(int(float(payload.get("open_positions", 0) or 0)), 0)
                    except (TypeError, ValueError):
                        strategy_positions[key] = 0
                if strategy_notional:
                    context["per_strategy_notional"].update(strategy_notional)
                if strategy_positions:
                    context["per_strategy_positions"].update(strategy_positions)

        if not context["per_symbol_notional"]:
            state = runtime.get_state()
            per_symbol = getattr(getattr(state, "risk", None), "current", None)
            positions = getattr(per_symbol, "position_usdt", {}) if per_symbol else {}
            if isinstance(positions, Mapping):
                context["per_symbol_notional"] = {
                    _normalise_symbol(symbol) or symbol: max(float(value or 0.0), 0.0)
                    for symbol, value in positions.items()
                }

        return context

    def _rotate_bucket(self, now: float) -> None:
        bucket = self._velocity.bucket_for(now)
        if self._current_bucket is None:
            self._current_bucket = bucket
            return
        if bucket != self._current_bucket:
            self._completed_buckets.append(self._bucket_violation)
            self._current_bucket = bucket
            self._bucket_violation = False

    def _check_caps(
        self,
        context: Mapping[str, object],
        strategy: str | None,
        symbol: str | None,
        notional: float,
        positions_delta: int,
    ) -> tuple[str | None, Dict[str, object]]:
        totals_notional = float(context.get("global_notional", 0.0) or 0.0)
        totals_positions = int(float(context.get("global_positions", 0) or 0))
        projected_notional = totals_notional + notional
        projected_positions = totals_positions + positions_delta

        limit = self._caps.global_notional
        if limit is not None and projected_notional > limit:
            return "CAP::GLOBAL_NOTIONAL", {
                "current": totals_notional,
                "projected": projected_notional,
                "limit": limit,
            }

        limit_positions = self._caps.max_open_positions_global
        if limit_positions is not None and projected_positions > limit_positions:
            return "CAP::GLOBAL_POSITIONS", {
                "current": totals_positions,
                "projected": projected_positions,
                "limit": limit_positions,
            }

        if strategy:
            per_strategy_notional = context.get("per_strategy_notional", {})
            current_strategy_notional = max(
                float(per_strategy_notional.get(strategy, 0.0) or 0.0),
                0.0,
            )
            limit_strategy_notional = self._caps.strategy_notional_limit(strategy)
            projected_strategy_notional = current_strategy_notional + notional
            if limit_strategy_notional is not None and projected_strategy_notional > limit_strategy_notional:
                return "CAP::STRATEGY_NOTIONAL", {
                    "strategy": strategy,
                    "current": current_strategy_notional,
                    "projected": projected_strategy_notional,
                    "limit": limit_strategy_notional,
                }

            per_strategy_positions = context.get("per_strategy_positions", {})
            current_positions = max(int(float(per_strategy_positions.get(strategy, 0) or 0)), 0)
            limit_strategy_positions = self._caps.strategy_position_limit(strategy)
            projected_strategy_positions = current_positions + positions_delta
            if limit_strategy_positions is not None and projected_strategy_positions > limit_strategy_positions:
                return "CAP::STRATEGY_POSITIONS", {
                    "strategy": strategy,
                    "current": current_positions,
                    "projected": projected_strategy_positions,
                    "limit": limit_strategy_positions,
                }

        if symbol:
            per_symbol_notional = context.get("per_symbol_notional", {})
            current_symbol_notional = max(
                float(per_symbol_notional.get(symbol, 0.0) or 0.0),
                0.0,
            )
            limit_symbol = self._caps.symbol_notional_limit(symbol)
            projected_symbol_notional = current_symbol_notional + notional
            if limit_symbol is not None and projected_symbol_notional > limit_symbol:
                return "CAP::SYMBOL_NOTIONAL", {
                    "symbol": symbol,
                    "current": current_symbol_notional,
                    "projected": projected_symbol_notional,
                    "limit": limit_symbol,
                }

        return None, {}

    def _check_velocity(self, operation: str, notional: float) -> tuple[str | None, Dict[str, object]]:
        delta_orders = 0
        delta_cancels = 0
        delta_notional = 0.0

        if operation == "cancel":
            delta_cancels = 1
        elif operation == "replace":
            delta_orders = 1
            delta_cancels = 1
            delta_notional = max(notional, 0.0)
        else:
            delta_orders = 1
            delta_notional = max(notional, 0.0)

        current_orders, current_cancels, current_notional = self._velocity.totals()
        projected_orders = current_orders + delta_orders
        projected_cancels = current_cancels + delta_cancels
        projected_notional = current_notional + delta_notional

        limit_orders = self._velocity_limits["orders"]
        if projected_orders > limit_orders:
            return "VELOCITY::ORDERS", {
                "current": current_orders,
                "projected": projected_orders,
                "limit": limit_orders,
            }

        limit_cancels = self._velocity_limits["cancels"]
        if projected_cancels > limit_cancels:
            return "VELOCITY::CANCELS", {
                "current": current_cancels,
                "projected": projected_cancels,
                "limit": limit_cancels,
            }

        limit_notional = self._velocity_limits["notional"]
        if projected_notional > limit_notional:
            return "VELOCITY::NOTIONAL", {
                "current": current_notional,
                "projected": projected_notional,
                "limit": limit_notional,
            }

        totals = self._velocity.record(
            orders=delta_orders,
            cancels=delta_cancels,
            notional=delta_notional,
        )
        self._update_velocity_metrics(*totals)
        return None, {}

    def _update_velocity_metrics(self, orders: int, cancels: int, notional: float) -> None:
        set_velocity("orders", orders)
        set_velocity("cancels", cancels)
        set_velocity("notional", notional)

    def _record_success(self, operation: str, notional: float) -> None:
        if operation != "cancel":
            totals = self._velocity.totals()
            self._update_velocity_metrics(*totals)

    def _record_violation(self, reason: str, details: Mapping[str, object]) -> None:
        self.throttled = True
        self.last_reason = reason
        self.last_violation = dict(details)
        self._bucket_violation = True
        set_risk_throttled(True, reason)
        try:
            runtime.update_risk_throttle(True, reason=reason, source="risk_governor_v2")
        except Exception:  # pragma: no cover - defensive
            pass
        totals = self._velocity.totals()
        self._update_velocity_metrics(*totals)


# ----------------------------------------------------------------------
# Pre-trade governor configuration helpers
# ----------------------------------------------------------------------


def _env_float_cap(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _env_int_cap(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _strategy_scope(value: str) -> str | None:
    return _normalise_strategy(value.replace("__", ":"))


def _symbol_scope(value: str) -> str | None:
    return _normalise_symbol(value.replace("__", ":"))


def _env_float_map(prefix: str, *, normaliser: Callable[[str], str | None]) -> Dict[str, float]:
    mapping: Dict[str, float] = {}
    base = _env_float_cap(prefix)
    if base is not None:
        mapping["__default__"] = base
    marker = f"{prefix}__"
    for key, value in os.environ.items():
        if not key.startswith(marker):
            continue
        scope_raw = key[len(marker) :]
        scope = normaliser(scope_raw)
        if not scope:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            mapping[scope] = numeric
    return mapping


def _env_int_map(prefix: str, *, normaliser: Callable[[str], str | None]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    base = _env_int_cap(prefix)
    if base is not None:
        mapping["__default__"] = base
    marker = f"{prefix}__"
    for key, value in os.environ.items():
        if not key.startswith(marker):
            continue
        scope_raw = key[len(marker) :]
        scope = normaliser(scope_raw)
        if not scope:
            continue
        try:
            numeric = int(float(value))
        except (TypeError, ValueError):
            continue
        if numeric > 0:
            mapping[scope] = numeric
    return mapping


def _caps_from_env() -> RiskCaps:
    return RiskCaps(
        global_notional=_env_float_cap("RISK_CAP_GLOBAL_NOTIONAL_USDT"),
        per_strategy_notional=_env_float_map(
            "RISK_CAP_PER_STRATEGY_NOTIONAL", normaliser=_strategy_scope
        ),
        per_symbol_notional=_env_float_map(
            "RISK_CAP_PER_SYMBOL_NOTIONAL", normaliser=_symbol_scope
        ),
        max_open_positions_global=_env_int_cap("RISK_CAP_MAX_OPEN_POSITIONS"),
        per_strategy_positions=_env_int_map(
            "RISK_CAP_PER_STRATEGY_POSITIONS", normaliser=_strategy_scope
        ),
    )


def _velocity_limits_from_env() -> Dict[str, float | None]:
    return {
        "orders": _env_float_cap("RISK_VELOCITY_MAX_ORDERS_PER_MIN"),
        "cancels": _env_float_cap("RISK_VELOCITY_MAX_CANCELS_PER_MIN"),
        "notional": _env_float_cap("RISK_VELOCITY_MAX_NOTIONAL_PER_MIN_USDT"),
    }


def _velocity_window_from_env(default: int = 60) -> int:
    raw = os.getenv("RISK_VELOCITY_WINDOW_SEC")
    if raw is None:
        return default
    try:
        numeric = int(float(str(raw).strip() or default))
    except (TypeError, ValueError):
        return default
    return max(numeric, 1)


_PRETRADE_GOVERNOR: RiskGovernor | None = None
_PRETRADE_LOCK = threading.RLock()


def _build_pretrade_governor_from_env() -> RiskGovernor:
    caps = _caps_from_env()
    limits = _velocity_limits_from_env()
    bucket = _velocity_window_from_env()
    return RiskGovernor(caps, velocity_limits=limits, bucket_s=bucket)


def configure_pretrade_risk_governor(
    *,
    caps: RiskCaps | None = None,
    velocity_limits: Mapping[str, float | int | None] | None = None,
    bucket_s: int | None = None,
    clock: Callable[[], float] | None = None,
) -> None:
    governor = RiskGovernor(
        caps or _caps_from_env(),
        velocity_limits=velocity_limits or _velocity_limits_from_env(),
        bucket_s=bucket_s or _velocity_window_from_env(),
        clock=clock,
    )
    with _PRETRADE_LOCK:
        global _PRETRADE_GOVERNOR
        _PRETRADE_GOVERNOR = governor


def get_pretrade_risk_governor() -> RiskGovernor:
    with _PRETRADE_LOCK:
        global _PRETRADE_GOVERNOR
        if _PRETRADE_GOVERNOR is None:
            _PRETRADE_GOVERNOR = _build_pretrade_governor_from_env()
        return _PRETRADE_GOVERNOR


def reset_pretrade_risk_governor_for_tests() -> None:
    with _PRETRADE_LOCK:
        global _PRETRADE_GOVERNOR
        _PRETRADE_GOVERNOR = None


@dataclass(frozen=True)
class RiskDecision:
    throttled: bool
    reason: str | None
    success_rate: float
    error_rate: float
    orders_total: int
    orders_ok: int
    orders_error: int
    window_started_at: float
    auto_hold_reason: str | None = None
    broker_state: str = STATE_UP
    broker_reason: str | None = None


@dataclass
class RiskGovernorConfig:
    window_sec: float = _DEFAULT_WINDOW_SEC
    min_success_rate: float = _DEFAULT_MIN_SUCCESS
    max_order_error_rate: float = _DEFAULT_MAX_ERROR
    min_broker_state: str = _DEFAULT_MIN_STATE
    hold_after_windows: int = _DEFAULT_HOLD_AFTER

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object] | None) -> "RiskGovernorConfig":
        if not isinstance(payload, Mapping):
            return cls()
        try:
            window_sec = float(payload.get("window_sec", _DEFAULT_WINDOW_SEC))
        except (TypeError, ValueError):
            window_sec = _DEFAULT_WINDOW_SEC
        try:
            min_success_rate = float(payload.get("min_success_rate", _DEFAULT_MIN_SUCCESS))
        except (TypeError, ValueError):
            min_success_rate = _DEFAULT_MIN_SUCCESS
        try:
            max_order_error_rate = float(payload.get("max_order_error_rate", _DEFAULT_MAX_ERROR))
        except (TypeError, ValueError):
            max_order_error_rate = _DEFAULT_MAX_ERROR
        min_state = str(payload.get("min_broker_state", _DEFAULT_MIN_STATE) or STATE_UP).upper()
        try:
            hold_after = int(payload.get("hold_after_windows", _DEFAULT_HOLD_AFTER))
        except (TypeError, ValueError):
            hold_after = _DEFAULT_HOLD_AFTER
        if window_sec < 60.0:
            window_sec = 60.0
        if min_success_rate <= 0 or min_success_rate > 1:
            min_success_rate = _DEFAULT_MIN_SUCCESS
        if max_order_error_rate < 0 or max_order_error_rate > 1:
            max_order_error_rate = _DEFAULT_MAX_ERROR
        if min_state not in _STATE_ORDER:
            min_state = _DEFAULT_MIN_STATE
        if hold_after <= 0:
            hold_after = _DEFAULT_HOLD_AFTER
        return cls(
            window_sec=window_sec,
            min_success_rate=min_success_rate,
            max_order_error_rate=max_order_error_rate,
            min_broker_state=min_state,
            hold_after_windows=hold_after,
        )


@dataclass
class _WindowHistoryEntry:
    start: float
    throttled: bool


class SlidingRiskGovernor:
    """Aggregate order outcomes and broker health into throttling decisions."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        config: RiskGovernorConfig | None = None,
    ) -> None:
        self._clock = clock or time.time
        self._config = config or RiskGovernorConfig()
        self._lock = threading.RLock()
        self._events: Deque[tuple[float, str, str]] = deque()
        self._error_breakdown: Counter[str] = Counter()
        self._current_window_start: float | None = None
        self._current_window_throttled: bool = False
        self._window_history: Deque[_WindowHistoryEntry] = deque(maxlen=max(self._config.hold_after_windows + 1, 4))
        self._last_snapshot: Dict[str, object] = {}
        self._last_throttle_reason: str | None = None
        self._last_auto_hold_window: float | None = None

    # ------------------------------------------------------------------
    # Recording helpers
    # ------------------------------------------------------------------
    def record_order_success(self, *, venue: str | None = None, category: str = _ORDER_OK) -> None:
        now = self._clock()
        with self._lock:
            self._events.append((now, _ORDER_OK, _normalise_category(category)))
            self._prune(now)
            if len(self._events) > 16384:
                self._events.popleft()

    def record_order_error(self, *, venue: str | None = None, category: str = _ORDER_ERROR) -> None:
        now = self._clock()
        normalised = _normalise_category(category)
        with self._lock:
            self._events.append((now, _ORDER_ERROR, normalised))
            self._error_breakdown[normalised] += 1
            self._prune(now)
            if len(self._events) > 16384:
                self._events.popleft()

    # ------------------------------------------------------------------
    def compute(self, *, venue: str | None = None) -> RiskDecision:
        now = self._clock()
        snapshot = get_broker_state()
        with self._lock:
            self._prune(now)
            orders_total, orders_ok, orders_error = self._counts()
            success_rate = orders_ok / orders_total if orders_total else 1.0
            error_rate = orders_error / orders_total if orders_total else 0.0
            broker_state, broker_reason = _resolve_broker_state(snapshot, venue)
            reason = self._decide_reason(success_rate, error_rate, broker_state, broker_reason)
            throttled = reason is not None
            auto_hold_reason = self._update_windows(now, throttled, reason)
            decision = RiskDecision(
                throttled=throttled,
                reason=reason,
                success_rate=success_rate,
                error_rate=error_rate,
                orders_total=orders_total,
                orders_ok=orders_ok,
                orders_error=orders_error,
                window_started_at=self._current_window_start or now,
                auto_hold_reason=auto_hold_reason,
                broker_state=broker_state,
                broker_reason=broker_reason,
            )
            self._update_metrics(decision)
            self._store_snapshot(decision, snapshot)
            return decision

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return dict(self._last_snapshot)

    # ------------------------------------------------------------------
    def _prune(self, now: float) -> None:
        window = self._config.window_sec
        threshold = now - window
        while self._events and self._events[0][0] < threshold:
            ts, kind, category = self._events.popleft()
            if kind == _ORDER_ERROR:
                try:
                    self._error_breakdown[category] -= 1
                    if self._error_breakdown[category] <= 0:
                        del self._error_breakdown[category]
                except KeyError:
                    pass

    def _counts(self) -> tuple[int, int, int]:
        total = len(self._events)
        errors = sum(1 for _, kind, _ in self._events if kind == _ORDER_ERROR)
        ok = total - errors
        return total, ok, errors

    def _decide_reason(
        self,
        success_rate: float,
        error_rate: float,
        broker_state: str,
        broker_reason: str | None,
    ) -> str | None:
        if success_rate < self._config.min_success_rate:
            return "LOW_SUCCESS_RATE"
        if error_rate > self._config.max_order_error_rate:
            return "HIGH_ORDER_ERRORS"
        if _STATE_ORDER.get(broker_state, 0) < _STATE_ORDER.get(self._config.min_broker_state, 0):
            if broker_reason:
                return f"BROKER_DEGRADED:{broker_reason}"
            return "BROKER_DEGRADED"
        return None

    def _update_windows(self, now: float, throttled: bool, reason: str | None) -> str | None:
        window_start = math.floor(now / self._config.window_sec) * self._config.window_sec
        auto_hold_reason: str | None = None
        if self._current_window_start is None:
            self._current_window_start = window_start
            self._current_window_throttled = throttled
        elif window_start != self._current_window_start:
            increment_risk_window(self._current_window_throttled)
            self._window_history.append(
                _WindowHistoryEntry(start=self._current_window_start, throttled=self._current_window_throttled)
            )
            self._current_window_start = window_start
            self._current_window_throttled = throttled
        else:
            self._current_window_throttled = self._current_window_throttled or throttled

        history: Deque[_WindowHistoryEntry] = deque(self._window_history, maxlen=self._window_history.maxlen)
        history.append(
            _WindowHistoryEntry(start=self._current_window_start, throttled=self._current_window_throttled)
        )
        if self._config.hold_after_windows > 0 and len(history) >= self._config.hold_after_windows:
            tail = list(history)[-self._config.hold_after_windows :]
            if all(entry.throttled for entry in tail):
                latest_window = tail[-1].start
                if latest_window != self._last_auto_hold_window:
                    auto_hold_reason = f"RISK::{reason or 'UNKNOWN'}"
                    self._last_auto_hold_window = latest_window
        return auto_hold_reason

    def _store_snapshot(self, decision: RiskDecision, snapshot: BrokerStateSnapshot) -> None:
        reason = decision.reason or ""
        if reason.startswith("BROKER_DEGRADED:"):
            reason = "BROKER_DEGRADED"
        payload: Dict[str, object] = {
            "throttled": decision.throttled,
            "reason": decision.reason,
            "success_rate_1h": decision.success_rate,
            "error_rate_1h": decision.error_rate,
            "orders_total": decision.orders_total,
            "orders_ok": decision.orders_ok,
            "orders_error": decision.orders_error,
            "window_started_at": decision.window_started_at,
            "broker_state": decision.broker_state,
            "broker_reason": decision.broker_reason,
            "min_success_rate": self._config.min_success_rate,
            "max_error_rate": self._config.max_order_error_rate,
            "min_broker_state": self._config.min_broker_state,
            "hold_after_windows": self._config.hold_after_windows,
            "window_sec": self._config.window_sec,
            "watchdog": snapshot.as_dict(),
            "error_breakdown": dict(self._error_breakdown),
        }
        self._last_snapshot = payload

    def _update_metrics(self, decision: RiskDecision) -> None:
        set_risk_success_rate(decision.success_rate)
        set_risk_error_rate(decision.error_rate)
        if self._last_throttle_reason and self._last_throttle_reason != decision.reason:
            set_risk_throttled(False, self._last_throttle_reason)
        set_risk_throttled(decision.throttled, decision.reason)
        self._last_throttle_reason = decision.reason


def _normalise_category(category: str) -> str:
    text = (category or "").strip().lower()
    return text or _ORDER_ERROR


def _resolve_broker_state(snapshot: BrokerStateSnapshot, venue: str | None) -> tuple[str, str | None]:
    venue_key = (venue or "").strip().lower() or None
    if venue_key is not None:
        state = snapshot.state_for(venue_key)
    else:
        state = snapshot.overall
    return state.state, state.reason


# ----------------------------------------------------------------------
# Singleton helpers
# ----------------------------------------------------------------------
_GOVERNOR_SINGLETON: SlidingRiskGovernor | None = None
_GOVERNOR_LOCK = threading.RLock()


def configure_risk_governor(*, clock: Callable[[], float] | None = None, config: Mapping[str, object] | None = None) -> None:
    """Initialise the risk governor singleton with the provided configuration."""

    settings = RiskGovernorConfig.from_mapping(config)
    instance = SlidingRiskGovernor(clock=clock, config=settings)
    with _GOVERNOR_LOCK:
        global _GOVERNOR_SINGLETON
        _GOVERNOR_SINGLETON = instance


def get_risk_governor() -> SlidingRiskGovernor:
    with _GOVERNOR_LOCK:
        global _GOVERNOR_SINGLETON
        if _GOVERNOR_SINGLETON is None:
            _GOVERNOR_SINGLETON = SlidingRiskGovernor()
        return _GOVERNOR_SINGLETON


def reset_risk_governor_for_tests() -> None:
    with _GOVERNOR_LOCK:
        global _GOVERNOR_SINGLETON
        _GOVERNOR_SINGLETON = None


# ----------------------------------------------------------------------
# Convenience recorders used by order flows/tests
# ----------------------------------------------------------------------

def record_order_success(*, venue: str | None = None, category: str = _ORDER_OK) -> None:
    governor = get_risk_governor()
    governor.record_order_success(venue=venue, category=category)


def record_order_error(*, venue: str | None = None, category: str = _ORDER_ERROR) -> None:
    governor = get_risk_governor()
    governor.record_order_error(venue=venue, category=category)


def evaluate_pre_trade(*, venue: str | None = None) -> RiskDecision:
    governor = get_risk_governor()
    decision = governor.compute(venue=venue)
    runtime.update_risk_throttle(decision.throttled, reason=decision.reason, source="risk_governor")
    if decision.auto_hold_reason:
        runtime.engage_safety_hold(decision.auto_hold_reason, source="risk_governor")
    state = runtime.get_state()
    safety = getattr(state, "safety", None)
    existing_snapshot: Dict[str, object]
    if safety is not None and isinstance(getattr(safety, "risk_snapshot", None), Mapping):
        existing_snapshot = dict(safety.risk_snapshot)
    else:
        existing_snapshot = {}
    existing_snapshot["governor"] = governor.snapshot()
    runtime.update_risk_snapshot(existing_snapshot)
    return decision


__all__ = [
    "RiskCaps",
    "VelocityWindow",
    "RiskGovernor",
    "RiskDecision",
    "RiskGovernorConfig",
    "SlidingRiskGovernor",
    "configure_pretrade_risk_governor",
    "configure_risk_governor",
    "get_pretrade_risk_governor",
    "get_risk_governor",
    "reset_pretrade_risk_governor_for_tests",
    "reset_risk_governor_for_tests",
    "record_order_success",
    "record_order_error",
    "evaluate_pre_trade",
]
