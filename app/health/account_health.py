"""Core models, evaluation helpers, metrics, and collectors for account health."""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal

from prometheus_client import CollectorRegistry, Gauge

try:  # pragma: no cover - optional during bootstrap
    from app.config.schema import HealthConfig
except Exception:  # pragma: no cover - fallback for optional import cycles
    HealthConfig = None  # type: ignore[misc, assignment]

AccountHealthState = Literal["OK", "WARN", "CRITICAL"]
_STATE_LABELS: tuple[AccountHealthState, ...] = ("OK", "WARN", "CRITICAL")

_EQUITY_KEYS = (
    "equity",
    "total_equity",
    "totalEquity",
    "wallet_balance",
    "walletBalance",
    "total_balance",
    "balance",
    "net_value",
    "netValue",
)
_FREE_COLLATERAL_KEYS = (
    "free_collateral",
    "freeCollateral",
    "available_balance",
    "availableBalance",
    "available",
    "free",
    "cash_bal",
    "cashBal",
)
_INIT_MARGIN_KEYS = (
    "init_margin",
    "initial_margin",
    "initialMargin",
    "total_initial_margin",
    "totalInitialMargin",
    "position_initial_margin",
    "positionInitialMargin",
    "posInitMargin",
)
_MAINT_MARGIN_KEYS = (
    "maint_margin",
    "maintenance_margin",
    "maintenanceMargin",
    "total_maintenance_margin",
    "totalMaintMargin",
    "mmr",
)
_MARGIN_RATIO_KEYS = (
    "margin_ratio",
    "marginRatio",
    "mgnRatio",
    "riskLevel",
    "risk_level",
)

_ACCOUNT_HEALTH_MARGIN_RATIO: Gauge | None = None
_ACCOUNT_HEALTH_FREE_COLLATERAL: Gauge | None = None
_ACCOUNT_HEALTH_STATE: Gauge | None = None


@dataclass(slots=True)
class AccountHealthSnapshot:
    """Snapshot of account-level margin health."""

    exchange: str
    equity_usdt: float
    free_collateral_usdt: float
    init_margin_usdt: float
    maint_margin_usdt: float
    margin_ratio: float
    ts: float


def _resolve_margin_ratio(snapshot: AccountHealthSnapshot) -> float:
    """Return a usable margin ratio for ``snapshot``."""

    ratio = float(snapshot.margin_ratio)
    if math.isnan(ratio):
        if snapshot.equity_usdt > 0.0:
            ratio = snapshot.maint_margin_usdt / snapshot.equity_usdt
        else:
            ratio = math.inf
    return max(0.0, ratio)


def _get_health_config(cfg: object) -> object:
    """Return the health config from ``cfg`` or defaults if missing."""

    health_cfg = getattr(cfg, "health", None)
    if health_cfg is not None:
        return health_cfg
    if HealthConfig is None:
        msg = "Health configuration missing and defaults unavailable"
        raise RuntimeError(msg)
    return HealthConfig()


def evaluate_health(snapshot: AccountHealthSnapshot, cfg: object) -> AccountHealthState:
    """Classify ``snapshot`` into an account health state."""

    health_cfg = _get_health_config(cfg)
    ratio = _resolve_margin_ratio(snapshot)

    if ratio >= float(health_cfg.margin_ratio_critical):
        return "CRITICAL"
    if snapshot.free_collateral_usdt <= float(
        health_cfg.free_collateral_critical_usd
    ):
        return "CRITICAL"
    if ratio >= float(health_cfg.margin_ratio_warn):
        return "WARN"
    if snapshot.free_collateral_usdt <= float(
        health_cfg.free_collateral_warn_usd
    ):
        return "WARN"
    return "OK"


def register_metrics(registry: CollectorRegistry) -> None:
    """Register Prometheus gauges for account health."""

    global _ACCOUNT_HEALTH_MARGIN_RATIO
    global _ACCOUNT_HEALTH_FREE_COLLATERAL
    global _ACCOUNT_HEALTH_STATE

    _ACCOUNT_HEALTH_MARGIN_RATIO = Gauge(
        "propbot_account_health_margin_ratio",
        "Account health margin ratio per exchange.",
        ("exchange",),
        registry=registry,
    )
    _ACCOUNT_HEALTH_FREE_COLLATERAL = Gauge(
        "propbot_account_health_free_collateral_usd",
        "Account health free collateral (USD) per exchange.",
        ("exchange",),
        registry=registry,
    )
    _ACCOUNT_HEALTH_STATE = Gauge(
        "propbot_account_health_state",
        "Account health state indicator per exchange.",
        ("exchange", "state"),
        registry=registry,
    )


def _require_metrics() -> tuple[Gauge, Gauge, Gauge]:
    if (
        _ACCOUNT_HEALTH_MARGIN_RATIO is None
        or _ACCOUNT_HEALTH_FREE_COLLATERAL is None
        or _ACCOUNT_HEALTH_STATE is None
    ):
        raise RuntimeError("Account health metrics have not been registered")
    return (
        _ACCOUNT_HEALTH_MARGIN_RATIO,
        _ACCOUNT_HEALTH_FREE_COLLATERAL,
        _ACCOUNT_HEALTH_STATE,
    )


def update_metrics(
    per_exchange: Mapping[str, AccountHealthSnapshot],
    states: Mapping[str, AccountHealthState],
) -> None:
    """Update account health gauges for the provided exchanges."""

    margin_ratio_gauge, collateral_gauge, state_gauge = _require_metrics()

    for exchange, snapshot in per_exchange.items():
        label = exchange or "unknown"
        ratio = _resolve_margin_ratio(snapshot)
        margin_ratio_gauge.labels(exchange=label).set(ratio)
        collateral_gauge.labels(exchange=label).set(float(snapshot.free_collateral_usdt))

    for exchange in _iter_exchanges(per_exchange.keys(), states.keys()):
        label = exchange or "unknown"
        state = (states.get(exchange) or "OK").upper()
        if state not in _STATE_LABELS:
            state = "OK"
        for candidate in _STATE_LABELS:
            state_gauge.labels(exchange=label, state=candidate).set(
                1.0 if candidate == state else 0.0
            )


def _iter_exchanges(*parts: Iterable[str]) -> set[str]:
    exchanges: set[str] = set()
    for part in parts:
        exchanges.update(part)
    return exchanges


def _coerce_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _iter_nested_candidates(value: object) -> Iterable[Mapping[str, object]]:
    queue: list[object] = [value]
    seen: set[int] = set()
    while queue:
        current = queue.pop()
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(current, Mapping):
            yield current
            for nested_key in ("raw", "data", "details", "info"):
                nested = current.get(nested_key)
                if nested is not None:
                    queue.append(nested)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)):
            queue.extend(current)


def _extract_metric(payload: Mapping[str, object] | Sequence[object] | None, keys: Iterable[str]) -> float | None:
    if payload is None:
        return None
    for candidate in _iter_nested_candidates(payload):
        for key in keys:
            if key in candidate:
                value = _coerce_float(candidate.get(key))
                if value is not None:
                    return value
    return None


def _fetch_adapter_payload(adapter: object) -> Mapping[str, object] | Sequence[object] | None:
    if adapter is None:
        return None
    if isinstance(adapter, Mapping):
        return adapter
    candidates = (
        "account_health_snapshot",
        "account_health",
        "collect_account_health",
        "account_snapshot",
        "get_account_health",
        "get_account_state",
        "snapshot",
        "state",
        "account_state",
        "data",
    )
    for name in candidates:
        attr = getattr(adapter, name, None)
        if attr is None:
            continue
        try:
            result = attr() if callable(attr) else attr
        except TypeError:
            continue
        if isinstance(result, (Mapping, Sequence)):
            return result
    return None


def _resolve_adapters(ctx: object) -> Mapping[str, object]:
    if ctx is None:
        return {}
    if isinstance(ctx, Mapping):
        return {str(key): value for key, value in ctx.items()}

    def _maybe_mapping(candidate: Any) -> Mapping[str, object] | None:
        if candidate is None:
            return None
        if isinstance(candidate, Mapping):
            return {str(key): value for key, value in candidate.items()}
        if callable(candidate):
            try:
                result = candidate()
            except TypeError:
                return None
            if isinstance(result, Mapping):
                return {str(key): value for key, value in result.items()}
        return None

    for attr in ("brokers", "adapters", "exchanges"):
        candidate = _maybe_mapping(getattr(ctx, attr, None))
        if candidate:
            return candidate

    router = getattr(ctx, "router", None)
    if router is not None:
        mapping = _resolve_adapters(router)
        if mapping:
            return mapping

    state = getattr(ctx, "state", None)
    derivatives = getattr(state, "derivatives", None) if state is not None else None
    venues = getattr(derivatives, "venues", None)
    if isinstance(venues, Mapping):
        mapping = {}
        for venue_id, runtime in venues.items():
            client = getattr(runtime, "client", None)
            if client is None:
                continue
            mapping[str(venue_id)] = client
        if mapping:
            return mapping
    return {}


def _resolve_config_scope(ctx: object) -> object:
    if ctx is None:
        if HealthConfig is not None:
            return SimpleNamespace(health=HealthConfig())
        return SimpleNamespace()
    candidates = [ctx]
    for attr in ("config", "state"):
        candidate = getattr(ctx, attr, None)
        if candidate is not None:
            candidates.append(candidate)
            data = getattr(candidate, "data", None)
            if data is not None:
                candidates.append(data)
    for candidate in candidates:
        if candidate is None:
            continue
        if getattr(candidate, "health", None) is not None:
            return candidate
    if HealthConfig is not None:
        return SimpleNamespace(health=HealthConfig())
    return ctx


def _build_snapshot(exchange: str, payload: Mapping[str, object] | Sequence[object], ts: float) -> AccountHealthSnapshot:
    equity = _extract_metric(payload, _EQUITY_KEYS) or 0.0
    free_collateral = _extract_metric(payload, _FREE_COLLATERAL_KEYS)
    if free_collateral is None:
        free_collateral = equity
    init_margin = _extract_metric(payload, _INIT_MARGIN_KEYS) or 0.0
    maint_margin = _extract_metric(payload, _MAINT_MARGIN_KEYS) or 0.0
    margin_ratio = _extract_metric(payload, _MARGIN_RATIO_KEYS)
    if margin_ratio is None:
        if equity > 0.0:
            margin_ratio = maint_margin / equity if equity else 0.0
        else:
            margin_ratio = math.inf
    snapshot = AccountHealthSnapshot(
        exchange=str(exchange),
        equity_usdt=float(equity),
        free_collateral_usdt=float(free_collateral),
        init_margin_usdt=float(init_margin),
        maint_margin_usdt=float(maint_margin),
        margin_ratio=float(margin_ratio),
        ts=float(ts),
    )
    return snapshot


def collect_account_health(ctx: object) -> dict[str, AccountHealthSnapshot]:
    """Collect per-exchange account health snapshots from broker adapters."""

    adapters = _resolve_adapters(ctx)
    if not adapters:
        update_metrics({}, {})
        return {}

    timestamp = time.time()
    snapshots: dict[str, AccountHealthSnapshot] = {}
    states: dict[str, AccountHealthState] = {}
    cfg_scope = _resolve_config_scope(ctx)

    for exchange, adapter in adapters.items():
        payload = _fetch_adapter_payload(adapter)
        if payload is None:
            continue
        snapshot = _build_snapshot(exchange, payload, timestamp)
        snapshots[exchange] = snapshot
        states[exchange] = evaluate_health(snapshot, cfg_scope)

    update_metrics(snapshots, states)
    return snapshots


__all__ = [
    "AccountHealthSnapshot",
    "AccountHealthState",
    "collect_account_health",
    "evaluate_health",
    "register_metrics",
    "update_metrics",
]
