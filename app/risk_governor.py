from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Dict, Iterable, Optional, Tuple

from .services import portfolio, risk, runtime

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GovernorLimits:
    """Snapshot of configured governor limits."""

    max_daily_loss_usd: float | None
    max_total_notional_usd: float | None
    max_unrealized_loss_usd: float | None
    clock_skew_hold_ms: float


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _env_float_default(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _load_limits() -> GovernorLimits:
    return GovernorLimits(
        max_daily_loss_usd=_env_float("MAX_DAILY_LOSS_USD"),
        max_total_notional_usd=_env_float("MAX_TOTAL_NOTIONAL_USD")
        or _env_float("MAX_TOTAL_NOTIONAL_USDT"),
        max_unrealized_loss_usd=_env_float("MAX_UNREALIZED_LOSS_USD"),
        clock_skew_hold_ms=_env_float_default("CLOCK_SKEW_HOLD_THRESHOLD_MS", 200.0),
    )


def _normalise_server_time(raw: float) -> float | None:
    if raw <= 0:
        return None
    if raw > 1e13:
        return raw / 1_000_000.0
    if raw > 1e10:
        return raw / 1_000.0
    return raw


def _collect_clock_skew_ms(state) -> float | None:
    runtime_state = getattr(state, "derivatives", None)
    venues = getattr(runtime_state, "venues", {}) or {}
    if not venues:
        return None
    now = time.time()
    samples: list[float] = []
    for runtime_entry in venues.values():
        client = getattr(runtime_entry, "client", None)
        if client is None or not hasattr(client, "server_time"):
            continue
        try:
            server_time_raw = client.server_time()
        except Exception:  # pragma: no cover - defensive
            continue
        try:
            numeric = float(server_time_raw)
        except (TypeError, ValueError):
            continue
        server_time = _normalise_server_time(numeric)
        if server_time is None:
            continue
        samples.append((server_time - now) * 1000.0)
    if not samples:
        return None
    return max(samples, key=lambda value: abs(value))


def _maintenance_flag(candidate) -> bool:
    if candidate is None:
        return False
    value = candidate
    if callable(candidate):  # pragma: no branch - simple accessor
        try:
            value = candidate()
        except Exception:  # pragma: no cover - defensive
            return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _check_maintenance(state) -> Tuple[bool, list[str]]:
    runtime_state = getattr(state, "derivatives", None)
    venues = getattr(runtime_state, "venues", {}) or {}
    maintenance: list[str] = []
    for venue_id, runtime_entry in venues.items():
        client = getattr(runtime_entry, "client", None)
        if client is None:
            continue
        flags = [
            getattr(client, "is_in_maintenance", None),
            getattr(client, "maintenance", None),
            getattr(client, "maintenance_mode", None),
            getattr(client, "readonly", None),
            getattr(client, "read_only", None),
            getattr(client, "is_readonly", None),
            getattr(client, "is_read_only", None),
        ]
        if any(_maintenance_flag(flag) for flag in flags):
            maintenance.append(str(venue_id))
    return bool(maintenance), maintenance


def _update_clock_skew_guard(skew_ms: float | None, threshold_ms: float) -> None:
    metrics = {}
    summary = "no samples"
    status = "OK"
    if skew_ms is not None:
        metrics = {"skew_ms": float(skew_ms), "threshold_ms": float(threshold_ms)}
        summary = f"|skew|={abs(skew_ms):.1f}ms"
        if abs(skew_ms) >= threshold_ms:
            status = "HOLD"
            summary = f"skew exceeded {threshold_ms:.0f}ms"
    runtime.update_guard("clock_skew", status, summary, metrics)


def _update_maintenance_guard(active: bool, venues: Iterable[str]) -> None:
    summary = "no window active"
    status = "OK"
    metrics: Dict[str, object] = {}
    if active:
        status = "HOLD"
        joined = ",".join(sorted(str(v) for v in venues)) or "unknown"
        summary = f"maintenance: {joined}"
        metrics = {"venues": joined}
    runtime.update_guard("maintenance_calendar", status, summary, metrics)


def _build_risk_snapshot(
    *,
    snapshot: portfolio.PortfolioSnapshot,
    risk_state,
    limits: GovernorLimits,
    skew_ms: float | None,
    maintenance_active: bool,
    maintenance_venues: Iterable[str],
) -> Dict[str, object]:
    exposures_by_venue: Dict[str, float] = {}
    exposures_by_symbol: Dict[str, float] = {}
    total_unrealized = 0.0
    for position in snapshot.positions:
        symbol = str(getattr(position, "symbol", "") or "")
        venue = str(getattr(position, "venue", "") or "")
        notional = float(getattr(position, "notional", 0.0) or 0.0)
        exposures_by_symbol[symbol] = exposures_by_symbol.get(symbol, 0.0) + abs(notional)
        exposures_by_venue[venue] = exposures_by_venue.get(venue, 0.0) + abs(notional)
        total_unrealized += float(getattr(position, "upnl", 0.0) or 0.0)

    daily_realized = float(getattr(getattr(risk_state, "current", SimpleNamespace()), "daily_loss_usdt", 0.0) or 0.0)

    payload = {
        "collected_ts": datetime.now(timezone.utc).isoformat(),
        "limits": {
            "max_daily_loss_usd": limits.max_daily_loss_usd,
            "max_total_notional_usd": limits.max_total_notional_usd,
            "max_unrealized_loss_usd": limits.max_unrealized_loss_usd,
            "clock_skew_hold_ms": limits.clock_skew_hold_ms,
        },
        "total_unrealized_pnl_usd": total_unrealized,
        "total_notional_usd": float(getattr(snapshot, "notional_total", 0.0) or 0.0),
        "daily_realized_pnl_usd": daily_realized,
        "exposure_by_symbol": exposures_by_symbol,
        "exposure_by_venue": exposures_by_venue,
        "clock_skew_ms": skew_ms,
        "maintenance_active": maintenance_active,
        "maintenance_venues": list(maintenance_venues),
    }
    return payload


def _evaluate_risk_limits(
    *,
    limits: GovernorLimits,
    snapshot: portfolio.PortfolioSnapshot,
    daily_realized: float,
    total_unrealized: float,
) -> Optional[str]:
    if limits.max_daily_loss_usd and limits.max_daily_loss_usd > 0:
        if daily_realized < -limits.max_daily_loss_usd:
            return "risk_limit breach: MAX_DAILY_LOSS_USD"
    if limits.max_total_notional_usd and limits.max_total_notional_usd > 0:
        if float(getattr(snapshot, "notional_total", 0.0) or 0.0) > limits.max_total_notional_usd:
            return "risk_limit breach: MAX_TOTAL_NOTIONAL_USD"
    if limits.max_unrealized_loss_usd and limits.max_unrealized_loss_usd > 0:
        unrealized_loss = max(-total_unrealized, 0.0)
        if unrealized_loss > limits.max_unrealized_loss_usd:
            return "risk_limit breach: MAX_UNREALIZED_LOSS_USD"
    return None


async def validate(*, context: str = "runtime") -> Optional[str]:
    """Validate risk posture and engage HOLD when limits are breached."""

    state = runtime.get_state()
    limits = _load_limits()
    maintenance_active, maintenance_venues = _check_maintenance(state)
    if maintenance_active:
        LOGGER.warning("maintenance mode detected", extra={"venues": maintenance_venues})
    _update_maintenance_guard(maintenance_active, maintenance_venues)

    snapshot = await portfolio.snapshot()
    risk_state = risk.refresh_runtime_state(snapshot=snapshot)

    skew_ms = _collect_clock_skew_ms(state)
    if skew_ms is not None:
        runtime.update_clock_skew(skew_ms / 1000.0, source="risk_governor")
    else:
        runtime.update_clock_skew(None, source="risk_governor")
    _update_clock_skew_guard(skew_ms, limits.clock_skew_hold_ms)

    risk_snapshot = _build_risk_snapshot(
        snapshot=snapshot,
        risk_state=risk_state,
        limits=limits,
        skew_ms=skew_ms,
        maintenance_active=maintenance_active,
        maintenance_venues=maintenance_venues,
    )
    runtime.update_risk_snapshot(risk_snapshot)

    reason: Optional[str] = None
    if maintenance_active:
        reason = "maintenance"

    dry_run_mode = bool(getattr(state.control, "dry_run_mode", False))
    total_unrealized = risk_snapshot["total_unrealized_pnl_usd"]
    daily_realized = risk_snapshot["daily_realized_pnl_usd"]
    if reason is None and not dry_run_mode:
        reason = _evaluate_risk_limits(
            limits=limits,
            snapshot=snapshot,
            daily_realized=daily_realized,
            total_unrealized=total_unrealized,
        )

    if reason is None and skew_ms is not None and abs(skew_ms) >= limits.clock_skew_hold_ms:
        reason = "clock_skew"

    if reason:
        engaged = runtime.engage_safety_hold(reason, source=f"risk_governor:{context}")
        if engaged:
            LOGGER.warning("risk governor engaged HOLD", extra={"reason": reason, "context": context})
        return reason
    return None
