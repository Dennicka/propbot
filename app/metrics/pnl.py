from __future__ import annotations

import math
import threading
from typing import Mapping

from prometheus_client import Gauge

__all__ = ["update_pnl_metrics", "reset_for_tests"]

_PNL_REALIZED = Gauge(
    "pnl_realized_usd",
    "Realised PnL (net of fees) by profile and symbol.",
    ("profile", "symbol"),
)

_PNL_UNREALIZED = Gauge(
    "pnl_unrealized_usd",
    "Unrealised PnL by profile and symbol.",
    ("profile", "symbol"),
)

_FEES_PAID = Gauge(
    "fees_paid_usd",
    "Trading fees paid (positive) or rebates received (negative) by profile and symbol.",
    ("profile", "symbol"),
)

_FUNDING_PAID = Gauge(
    "funding_paid_usd",
    "Funding impact by profile and symbol (positive when received).",
    ("profile", "symbol"),
)

_KNOWN_KEYS = {
    _PNL_REALIZED: set(),
    _PNL_UNREALIZED: set(),
    _FEES_PAID: set(),
    _FUNDING_PAID: set(),
}

_LOCK = threading.RLock()


def _normalise_profile(value: str | None) -> str:
    text = (value or "").strip().lower()
    return text or "unknown"


def _normalise_symbol(value: str | None) -> str:
    text = (value or "").strip().upper()
    return text or "UNKNOWN"


def _safe_value(value: object) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(numeric) or math.isinf(numeric):
        return 0.0
    return numeric


def _update_gauge(
    gauge: Gauge,
    profile: str,
    payload: Mapping[str, object],
    *,
    total: float | None = None,
) -> None:
    registry = _KNOWN_KEYS[gauge]
    seen: set[tuple[str, str]] = set()
    for symbol, raw in payload.items():
        symbol_label = _normalise_symbol(symbol)
        value = _safe_value(raw)
        gauge.labels(profile=profile, symbol=symbol_label).set(value)
        seen.add((profile, symbol_label))
    if total is not None:
        total_value = _safe_value(total)
        gauge.labels(profile=profile, symbol="__total__").set(total_value)
        seen.add((profile, "__total__"))
    stale = {key for key in registry if key[0] == profile and key not in seen}
    for label in stale:
        gauge.labels(profile=label[0], symbol=label[1]).set(0.0)
        registry.remove(label)
    registry.update(seen)


def update_pnl_metrics(
    *,
    profile: str,
    realized: Mapping[str, object] | None,
    unrealized: Mapping[str, object] | None,
    fees: Mapping[str, object] | None,
    funding: Mapping[str, object] | None,
    total_realized: float | None = None,
    total_unrealized: float | None = None,
    total_fees: float | None = None,
    total_funding: float | None = None,
) -> None:
    """Expose the latest PnL breakdown via Prometheus gauges."""

    profile_label = _normalise_profile(profile)
    with _LOCK:
        if realized is not None:
            _update_gauge(_PNL_REALIZED, profile_label, realized, total=total_realized)
        if unrealized is not None:
            _update_gauge(_PNL_UNREALIZED, profile_label, unrealized, total=total_unrealized)
        if fees is not None:
            _update_gauge(_FEES_PAID, profile_label, fees, total=total_fees)
        if funding is not None:
            _update_gauge(_FUNDING_PAID, profile_label, funding, total=total_funding)


def reset_for_tests() -> None:
    """Reset tracked label state for deterministic tests."""

    with _LOCK:
        for gauge, registry in _KNOWN_KEYS.items():
            for profile, symbol in tuple(registry):
                gauge.labels(profile=profile, symbol=symbol).set(0.0)
            registry.clear()
