from __future__ import annotations

import math
import threading
from typing import Iterable, Mapping

from prometheus_client import Gauge

__all__ = ["update_pnl_metrics", "publish_daily_snapshots", "reset_for_tests"]

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

_DAILY_REALIZED = Gauge(
    "pnl_daily_realized_usd",
    "Realised PnL aggregated by UTC date.",
    ("date",),
)

_DAILY_FEES = Gauge(
    "pnl_daily_fees_usd",
    "Fees paid aggregated by UTC date (positive for fees).",
    ("date",),
)

_DAILY_FUNDING = Gauge(
    "pnl_daily_funding_usd",
    "Funding impact aggregated by UTC date.",
    ("date",),
)

_DAILY_NET = Gauge(
    "pnl_daily_net_usd",
    "Net realised PnL aggregated by UTC date.",
    ("date",),
)

_KNOWN_KEYS = {
    _PNL_REALIZED: set(),
    _PNL_UNREALIZED: set(),
    _FEES_PAID: set(),
    _FUNDING_PAID: set(),
    _DAILY_REALIZED: set(),
    _DAILY_FEES: set(),
    _DAILY_FUNDING: set(),
    _DAILY_NET: set(),
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


def publish_daily_snapshots(snapshots: Iterable[object]) -> None:
    """Expose ledger daily snapshots via Prometheus gauges."""

    with _LOCK:
        seen_dates: dict[Gauge, set[tuple[str, ...]]] = {
            _DAILY_REALIZED: set(),
            _DAILY_FEES: set(),
            _DAILY_FUNDING: set(),
            _DAILY_NET: set(),
        }
        for snapshot in snapshots:
            date = getattr(snapshot, "date", None)
            if date is None and isinstance(snapshot, Mapping):
                date = snapshot.get("date")
            date_str = str(date or "").strip()
            if not date_str:
                continue
            realized = getattr(snapshot, "realized_pnl", None)
            if realized is None and isinstance(snapshot, Mapping):
                realized = snapshot.get("realized_pnl")
            fees = getattr(snapshot, "fees", None)
            if fees is None and isinstance(snapshot, Mapping):
                fees = snapshot.get("fees")
            funding = getattr(snapshot, "funding", None)
            if funding is None and isinstance(snapshot, Mapping):
                funding = snapshot.get("funding")
            rebates = getattr(snapshot, "rebates", None)
            if rebates is None and isinstance(snapshot, Mapping):
                rebates = snapshot.get("rebates")
            net = getattr(snapshot, "net_pnl", None)
            if net is None and isinstance(snapshot, Mapping):
                net = snapshot.get("net_pnl")

            _DAILY_REALIZED.labels(date=date_str).set(_safe_value(realized))
            seen_dates[_DAILY_REALIZED].add((date_str,))
            _DAILY_FEES.labels(date=date_str).set(_safe_value(fees))
            seen_dates[_DAILY_FEES].add((date_str,))
            _DAILY_FUNDING.labels(date=date_str).set(_safe_value(funding))
            seen_dates[_DAILY_FUNDING].add((date_str,))
            net_value = _safe_value(net)
            if net is None and realized is not None:
                net_value = _safe_value(realized) - _safe_value(fees) + _safe_value(funding) + _safe_value(rebates)
            _DAILY_NET.labels(date=date_str).set(net_value)
            seen_dates[_DAILY_NET].add((date_str,))

        for gauge in (_DAILY_REALIZED, _DAILY_FEES, _DAILY_FUNDING, _DAILY_NET):
            registry = _KNOWN_KEYS[gauge]
            stale = registry - seen_dates[gauge]
            for date in stale:
                gauge.labels(date=date[0]).set(0.0)
                registry.discard(date)
            registry.update(seen_dates[gauge])


def reset_for_tests() -> None:
    """Reset tracked label state for deterministic tests."""

    with _LOCK:
        for gauge, registry in _KNOWN_KEYS.items():
            if gauge in (_DAILY_REALIZED, _DAILY_FEES, _DAILY_FUNDING, _DAILY_NET):
                for (date,) in tuple(registry):
                    gauge.labels(date=date).set(0.0)
                    registry.discard((date,))
            else:
                for profile, symbol in tuple(registry):
                    gauge.labels(profile=profile, symbol=symbol).set(0.0)
                    registry.discard((profile, symbol))
