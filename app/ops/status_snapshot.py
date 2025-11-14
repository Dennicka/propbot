from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from app.alerts.registry import AlertRecord, alerts_to_dict

from app.config import feature_flags

try:
    from app.risk.daily_loss import get_daily_loss_cap_state
except Exception:  # pragma: no cover - defensive import guard
    get_daily_loss_cap_state = None  # type: ignore[assignment]


@dataclass
class RouterStatus:
    mode: str
    safe_mode: bool
    profile: str
    ff_pretrade_strict: bool
    ff_risk_limits: bool


@dataclass
class RiskStatus:
    daily_loss_cap: float | None
    daily_loss_pnl: float | None
    daily_loss_remaining: float | None
    notional_caps: Mapping[str, float]
    notional_used: Mapping[str, float]
    notional_remaining: Mapping[str, float]


@dataclass
class ReadinessStatus:
    live_ready: bool
    last_check_ts: float | None
    last_reason: str | None


@dataclass
class MarketDataStatus:
    healthy: bool
    stale_symbols: Sequence[str]


@dataclass
class AlertsStatus:
    last_n: Sequence[Mapping[str, Any]]


@dataclass
class OpsSnapshot:
    router: RouterStatus
    risk: RiskStatus
    readiness: ReadinessStatus
    market_data: MarketDataStatus
    alerts: AlertsStatus


def _safe_router_state(router: Any) -> tuple[str, bool, str]:
    mode = "UNKNOWN"
    safe_mode = False
    profile = "unknown"
    if router is None:
        return mode, safe_mode, profile
    try:
        state = router.get_state()
    except Exception:  # pragma: no cover - defensive
        state = None
    if state is not None:
        control = getattr(state, "control", None)
        if control is not None:
            mode = str(getattr(control, "mode", mode) or mode)
            safe_mode = bool(getattr(control, "safe_mode", safe_mode))
    try:
        profile_obj = router.get_profile()
    except Exception:  # pragma: no cover - defensive
        profile_obj = None
    if profile_obj is not None:
        profile = str(getattr(profile_obj, "name", profile) or profile)
    elif state is not None:
        control = getattr(state, "control", None)
        deployment_mode = getattr(control, "deployment_mode", None)
        if deployment_mode:
            profile = str(deployment_mode)
    return mode, safe_mode, profile


def _safe_feature_flags() -> tuple[bool, bool]:
    try:
        pretrade = feature_flags.pretrade_strict_on()
    except Exception:  # pragma: no cover - defensive
        pretrade = False
    try:
        risk_limits = feature_flags.risk_limits_on()
    except Exception:  # pragma: no cover - defensive
        risk_limits = False
    return pretrade, risk_limits


def _normalise_notional(mapping: Any) -> dict[str, float]:
    result: dict[str, float] = {}
    if not isinstance(mapping, Mapping):
        return result
    for key, value in mapping.items():
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        result[str(key)] = numeric
    return result


def _compute_notional_remaining(
    caps: Mapping[str, float], used: Mapping[str, float]
) -> dict[str, float]:
    remaining: dict[str, float] = {}
    for venue, limit in caps.items():
        used_value = abs(float(used.get(venue, 0.0)))
        remaining[venue] = max(float(limit) - used_value, 0.0)
    return remaining


def _daily_loss_from_state(state: Any) -> tuple[float | None, float | None, float | None]:
    daily_cap = None
    daily_pnl = None
    daily_remaining = None
    risk_state = getattr(state, "risk", None)
    limits = getattr(risk_state, "limits", None)
    if limits is not None:
        daily_cap_value = getattr(limits, "max_daily_loss_usdt", None)
        if daily_cap_value is not None:
            try:
                daily_cap = float(daily_cap_value)
            except (TypeError, ValueError):
                daily_cap = None
    current = getattr(risk_state, "current", None)
    if current is not None:
        pnl_value = getattr(current, "daily_loss_usdt", None)
        if pnl_value is not None:
            try:
                daily_pnl = float(pnl_value)
            except (TypeError, ValueError):
                daily_pnl = None
    if daily_cap is not None and daily_pnl is not None:
        losses = max(-daily_pnl, 0.0)
        daily_remaining = max(daily_cap - losses, 0.0)
    if daily_cap is None or daily_pnl is None:
        provider = get_daily_loss_cap_state
        if provider is not None:
            try:
                snapshot = provider()  # type: ignore[misc]
            except Exception:  # pragma: no cover - defensive
                snapshot = None
            if isinstance(snapshot, Mapping):
                cap_value = snapshot.get("max_daily_loss_usdt") or snapshot.get("cap_usdt")
                pnl_value = snapshot.get("realized_pnl_today_usdt")
                remaining_value = snapshot.get("remaining_usdt")
                try:
                    daily_cap = float(cap_value) if cap_value is not None else daily_cap
                except (TypeError, ValueError):
                    pass
                try:
                    daily_pnl = float(pnl_value) if pnl_value is not None else daily_pnl
                except (TypeError, ValueError):
                    pass
                try:
                    if remaining_value is not None:
                        daily_remaining = float(remaining_value)
                except (TypeError, ValueError):
                    pass
    return daily_cap, daily_pnl, daily_remaining


def _risk_snapshot(state: Any) -> RiskStatus:
    risk_state = getattr(state, "risk", None)
    limits = getattr(risk_state, "limits", None)
    current = getattr(risk_state, "current", None)
    caps = _normalise_notional(getattr(limits, "max_position_usdt", {}))
    used = _normalise_notional(getattr(current, "position_usdt", {}))
    remaining = _compute_notional_remaining(caps, used)
    daily_cap, daily_pnl, daily_remaining = _daily_loss_from_state(state)
    return RiskStatus(
        daily_loss_cap=daily_cap,
        daily_loss_pnl=daily_pnl,
        daily_loss_remaining=daily_remaining,
        notional_caps=caps,
        notional_used=used,
        notional_remaining=remaining,
    )


def _readiness_snapshot(registry: Any) -> ReadinessStatus:
    live_ready = False
    last_check_ts: float | None = None
    last_reason: str | None = None
    if registry is None:
        return ReadinessStatus(live_ready=False, last_check_ts=None, last_reason=None)
    try:
        status, components = registry.report()
    except Exception:  # pragma: no cover - defensive
        status, components = "unknown", {}
    status_text = str(status or "").strip().lower()
    live_ready = status_text == "ready"
    if hasattr(registry, "items"):
        try:
            timestamps = [
                float(getattr(probe, "ts"))
                for probe in registry.items.values()
                if probe is not None
            ]
        except Exception:  # pragma: no cover - defensive
            timestamps = []
        if timestamps:
            last_check_ts = max(timestamps)
    if isinstance(components, Mapping):
        stale_reasons: list[str] = []
        fallback_reason: str | None = None
        for name, entry in components.items():
            if not isinstance(entry, Mapping):
                continue
            reason = entry.get("reason")
            if reason:
                fallback_reason = str(reason)
            stale = entry.get("stale")
            if bool(stale):
                stale_reasons.append(str(name))
        if stale_reasons:
            last_reason = f"stale:{stale_reasons[0]}"
        elif fallback_reason:
            last_reason = fallback_reason
        elif not live_ready:
            last_reason = status_text or None
    elif not live_ready:
        last_reason = status_text or None
    return ReadinessStatus(
        live_ready=live_ready,
        last_check_ts=last_check_ts,
        last_reason=last_reason,
    )


def _market_data_snapshot(watchdog: Any) -> MarketDataStatus:
    healthy = True
    stale_symbols: list[str] = []
    if watchdog is None:
        return MarketDataStatus(healthy=True, stale_symbols=())
    try:
        snapshot = watchdog.report()
    except Exception:  # pragma: no cover - defensive
        snapshot = None
    if isinstance(snapshot, Mapping):
        for venue, symbols in snapshot.items():
            if not isinstance(symbols, Mapping):
                continue
            for symbol, payload in symbols.items():
                if isinstance(payload, Mapping) and bool(payload.get("stale")):
                    healthy = False
                    stale_symbols.append(f"{venue}:{symbol}")
    return MarketDataStatus(healthy=healthy, stale_symbols=tuple(stale_symbols))


def _alerts_snapshot(registry: Any, *, limit: int = 10) -> AlertsStatus:
    entries: Sequence[Mapping[str, Any]]
    if registry is None:
        entries = ()
    else:
        try:
            if hasattr(registry, "last"):
                entries = registry.last(limit)  # type: ignore[assignment]
            elif hasattr(registry, "recent"):
                entries = registry.recent(limit)  # type: ignore[assignment]
            else:
                entries = registry.get_last(limit)  # type: ignore[assignment]
        except Exception:  # pragma: no cover - defensive
            entries = ()
    normalised: list[Mapping[str, Any]] = []
    for entry in entries:
        if isinstance(entry, AlertRecord):
            normalised.append(alerts_to_dict([entry])[0])
        elif isinstance(entry, Mapping):
            normalised.append(dict(entry))
    return AlertsStatus(last_n=tuple(normalised))


def build_ops_snapshot(
    *,
    router: Any,
    risk_governor: Any,
    readiness_registry: Any,
    market_watchdog: Any,
    alerts_registry: Any,
) -> OpsSnapshot:
    state = None
    if router is not None:
        try:
            state = router.get_state()
        except Exception:  # pragma: no cover - defensive
            state = None
    _ = risk_governor  # reserved for future metrics, avoids unused-argument lint
    mode, safe_mode, profile = _safe_router_state(router)
    ff_pretrade_strict, ff_risk_limits = _safe_feature_flags()
    risk = _risk_snapshot(state)
    readiness = _readiness_snapshot(readiness_registry)
    market = _market_data_snapshot(market_watchdog)
    alerts = _alerts_snapshot(alerts_registry)
    router_status = RouterStatus(
        mode=mode,
        safe_mode=safe_mode,
        profile=profile,
        ff_pretrade_strict=ff_pretrade_strict,
        ff_risk_limits=ff_risk_limits,
    )
    return OpsSnapshot(
        router=router_status,
        risk=risk,
        readiness=readiness,
        market_data=market,
        alerts=alerts,
    )


def ops_snapshot_to_dict(snapshot: OpsSnapshot) -> dict[str, Any]:
    return asdict(snapshot)
