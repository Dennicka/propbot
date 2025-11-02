"""Core models, evaluation helpers, and metrics for account health."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Literal, Mapping

from prometheus_client import CollectorRegistry, Gauge

try:  # pragma: no cover - optional during bootstrap
    from app.config.schema import HealthConfig
except Exception:  # pragma: no cover - fallback for optional import cycles
    HealthConfig = None  # type: ignore[misc, assignment]

AccountHealthState = Literal["OK", "WARN", "CRITICAL"]
_STATE_LABELS: tuple[AccountHealthState, ...] = ("OK", "WARN", "CRITICAL")

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
    if not math.isfinite(ratio) or math.isnan(ratio):
        ratio = (
            snapshot.maint_margin_usdt / snapshot.equity_usdt
            if snapshot.equity_usdt > 0.0
            else 1.0
        )
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


__all__ = [
    "AccountHealthSnapshot",
    "AccountHealthState",
    "evaluate_health",
    "register_metrics",
    "update_metrics",
]
