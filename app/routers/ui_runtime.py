from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.runtime.live_guard import LiveTradingGuard
from app.runtime.promotion import PromotionStage, get_promotion_status

try:  # pragma: no cover - optional settings module
    from app.settings import settings as app_settings
except ImportError:  # pragma: no cover - optional settings module
    app_settings = None
from app.services.runtime import get_profile

router = APIRouter(prefix="/api/ui", tags=["ui", "runtime"])


class UiLiveGuardConfig(BaseModel):
    runtime_profile: str
    state: Literal["disabled", "enabled", "test_only"]
    allow_live_trading: bool
    allowed_venues: list[str]
    allowed_strategies: list[str]
    reason: str | None = None
    promotion_stage: str | None = None
    promotion_reason: str | None = None
    promotion_allowed_next_stages: list[str] | None = None
    approvals_enabled: bool | None = None
    approvals_last_request_id: str | None = None
    approvals_last_action: str | None = None
    approvals_last_status: str | None = None
    approvals_last_updated_at: datetime | None = None
    approvals_requestor_id: str | None = None
    approvals_approver_id: str | None = None
    approvals_resolution_reason: str | None = None


class UiPromotionStatus(BaseModel):
    stage: PromotionStage
    runtime_profile: str
    is_live_profile: bool
    allowed_next_stages: list[PromotionStage]
    reason: str | None = None


def get_live_guard() -> LiveTradingGuard:
    profile = get_profile()
    return LiveTradingGuard(runtime_profile=profile.name)


@router.get("/live-guard", response_model=UiLiveGuardConfig)
async def get_live_guard_config(
    live_guard: LiveTradingGuard = Depends(get_live_guard),
) -> UiLiveGuardConfig:
    cfg = live_guard.get_config_view()
    promotion = get_promotion_status(app_settings)
    return UiLiveGuardConfig(
        runtime_profile=cfg.runtime_profile,
        state=cfg.state,
        allow_live_trading=cfg.allow_live_trading,
        allowed_venues=list(cfg.allowed_venues),
        allowed_strategies=list(cfg.allowed_strategies),
        reason=cfg.reason,
        promotion_stage=promotion.stage,
        promotion_reason=promotion.reason,
        promotion_allowed_next_stages=list(promotion.allowed_next_stages),
        approvals_enabled=cfg.approvals_enabled,
        approvals_last_request_id=cfg.approvals_last_request_id,
        approvals_last_action=cfg.approvals_last_action,
        approvals_last_status=cfg.approvals_last_status,
        approvals_last_updated_at=cfg.approvals_last_updated_at,
        approvals_requestor_id=cfg.approvals_requestor_id,
        approvals_approver_id=cfg.approvals_approver_id,
        approvals_resolution_reason=cfg.approvals_resolution_reason,
    )


@router.get("/live-promotion", response_model=UiPromotionStatus)
async def get_live_promotion_status() -> UiPromotionStatus:
    status = get_promotion_status(app_settings)
    return UiPromotionStatus(
        stage=status.stage,
        runtime_profile=status.runtime_profile,
        is_live_profile=status.is_live_profile,
        allowed_next_stages=list(status.allowed_next_stages),
        reason=status.reason,
    )
