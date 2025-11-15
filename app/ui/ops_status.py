from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from fastapi import FastAPI
from pydantic import BaseModel

from app.config.profile import is_live
from app.runtime.live_guard import LiveTradingGuard
from app.runtime.promotion import get_promotion_status
from app.risk.daily_loss import is_daily_loss_cap_breached
from app.services.health_state import evaluate_health
from app.services.live_readiness import compute_readiness
from app.services.runtime import get_profile

try:  # pragma: no cover - optional settings module
    from app.settings import settings as app_settings
except ImportError:  # pragma: no cover - optional settings module
    app_settings = None


class OpsStatusSnapshot(BaseModel):
    runtime_profile: str
    is_live_profile: bool

    health_ok: bool
    readiness_ok: bool

    market_data_ok: bool | None = None
    live_trading_allowed: bool | None = None
    pnl_cap_hit: bool | None = None
    live_approvals_enabled: bool | None = None
    live_approvals_last_status: str | None = None

    health_reason: str | None = None
    readiness_reason: str | None = None
    live_trading_reason: str | None = None
    promotion_stage: str | None = None
    promotion_reason: str | None = None
    promotion_allowed_next_stages: list[str] | None = None


def _join_reasons(reasons: Iterable[str]) -> str | None:
    cleaned = [reason for reason in (reason.strip() for reason in reasons) if reason]
    if not cleaned:
        return None
    return ", ".join(cleaned)


def _health_reason(snapshot: Mapping[str, Any]) -> str | None:
    issues: list[str] = []
    if not bool(snapshot.get("resume_ok", True)):
        issues.append("resume:not_ok")
    if not bool(snapshot.get("journal_ok", True)):
        issues.append("journal:not_ok")
    if not bool(snapshot.get("auto_ok", True)):
        issues.append("auto_hedge:not_ok")
    if not bool(snapshot.get("scanner_ok", True)):
        issues.append("scanner:not_ok")
    if not bool(snapshot.get("config_ok", True)):
        issues.append("config:not_ok")
        config_errors = snapshot.get("config_errors")
        if isinstance(config_errors, Sequence) and not isinstance(config_errors, (str, bytes)):
            issues.extend(str(error) for error in config_errors if error)
    if not bool(snapshot.get("leader", True)):
        issues.append("leader:not_ok")
    return _join_reasons(issues)


def _readiness_reason(snapshot: Mapping[str, Any]) -> str | None:
    reasons = snapshot.get("reasons")
    if isinstance(reasons, Sequence):
        return _join_reasons(str(reason) for reason in reasons if reason is not None)
    return None


def _market_data_ok(snapshot: Mapping[str, Any]) -> bool | None:
    watchdog = snapshot.get("watchdog")
    if not isinstance(watchdog, Mapping):
        return None
    components = watchdog.get("components")
    if isinstance(components, Mapping):
        market_component = components.get("marketdata")
        if isinstance(market_component, Mapping):
            level = str(market_component.get("level") or "").lower()
            if level in {"ok", "warn", "fail"}:
                return level == "ok"
    overall = watchdog.get("overall")
    if isinstance(overall, str):
        level = overall.lower()
        if level in {"ok", "warn", "fail"}:
            return level == "ok"
    return None


async def build_ops_status_snapshot(*, app: FastAPI | None = None) -> OpsStatusSnapshot:
    profile = get_profile()
    runtime_profile = profile.name
    live_profile = is_live(profile)

    health_snapshot = evaluate_health(app)
    health_ok = bool(health_snapshot.get("ok"))
    health_reason = _health_reason(health_snapshot) if not health_ok else None

    readiness_snapshot = compute_readiness(app)
    readiness_ok = bool(readiness_snapshot.get("ready"))
    readiness_reason = _readiness_reason(readiness_snapshot) if not readiness_ok else None
    market_data_ok = _market_data_ok(readiness_snapshot)

    guard = LiveTradingGuard(runtime_profile=runtime_profile)
    guard_view = guard.get_config_view()
    live_allowed = guard_view.allow_live_trading and guard_view.state == "enabled" and live_profile
    if live_allowed:
        live_reason = None
    else:
        live_reason = guard_view.reason or guard_view.state

    promotion = get_promotion_status(app_settings)

    return OpsStatusSnapshot(
        runtime_profile=runtime_profile,
        is_live_profile=live_profile,
        health_ok=health_ok,
        readiness_ok=readiness_ok,
        market_data_ok=market_data_ok,
        live_trading_allowed=live_allowed,
        pnl_cap_hit=bool(is_daily_loss_cap_breached()),
        live_approvals_enabled=guard_view.approvals_enabled,
        live_approvals_last_status=guard_view.approvals_last_status,
        health_reason=health_reason,
        readiness_reason=readiness_reason,
        live_trading_reason=live_reason,
        promotion_stage=promotion.stage,
        promotion_reason=promotion.reason,
        promotion_allowed_next_stages=list(promotion.allowed_next_stages),
    )


__all__ = ["OpsStatusSnapshot", "build_ops_status_snapshot"]
