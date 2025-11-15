from __future__ import annotations

from pydantic import BaseModel

from app.config.profile import is_live
from app.runtime.live_guard import LiveGuardConfigView, LiveTradingGuard
from app.services.runtime import get_profile


class LiveSafetySnapshot(BaseModel):
    """Serialized snapshot of live trading safety state for UI dashboards."""

    runtime_profile: str
    is_live_profile: bool
    live_trading_guard_state: str
    live_trading_allowed: bool
    reason: str | None = None


def _derive_guard_state(config: LiveGuardConfigView) -> str:
    if config.state == "enabled" and config.allow_live_trading:
        return "allowed"
    if config.state == "test_only":
        return "test_only"
    if not config.allow_live_trading:
        return "blocked"
    return config.state


def build_live_safety_snapshot(
    *,
    live_guard: LiveTradingGuard | None = None,
) -> LiveSafetySnapshot:
    profile = get_profile()
    guard = live_guard or LiveTradingGuard(runtime_profile=profile.name)
    config = guard.get_config_view()

    live_allowed = config.allow_live_trading and config.state == "enabled" and is_live(profile)

    return LiveSafetySnapshot(
        runtime_profile=profile.name,
        is_live_profile=is_live(profile),
        live_trading_guard_state=_derive_guard_state(config),
        live_trading_allowed=live_allowed,
        reason=config.reason if not live_allowed else None,
    )
