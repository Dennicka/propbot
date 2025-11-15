from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.runtime.live_guard import LiveTradingGuard
from app.services.runtime import get_profile

router = APIRouter(prefix="/api/ui", tags=["ui", "runtime"])


class UiLiveGuardConfig(BaseModel):
    runtime_profile: str
    state: Literal["disabled", "enabled", "test_only"]
    allow_live_trading: bool
    allowed_venues: list[str]
    allowed_strategies: list[str]
    reason: str | None = None


def get_live_guard() -> LiveTradingGuard:
    profile = get_profile()
    return LiveTradingGuard(runtime_profile=profile.name)


@router.get("/live-guard", response_model=UiLiveGuardConfig)
async def get_live_guard_config(
    live_guard: LiveTradingGuard = Depends(get_live_guard),
) -> UiLiveGuardConfig:
    cfg = live_guard.get_config_view()
    return UiLiveGuardConfig(
        runtime_profile=cfg.runtime_profile,
        state=cfg.state,
        allow_live_trading=cfg.allow_live_trading,
        allowed_venues=list(cfg.allowed_venues),
        allowed_strategies=list(cfg.allowed_strategies),
        reason=cfg.reason,
    )
