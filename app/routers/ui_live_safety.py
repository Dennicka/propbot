from __future__ import annotations

from fastapi import APIRouter

from app.runtime.live_guard import LiveTradingGuard
from app.services.runtime import get_profile
from app.ui.live_safety import LiveSafetySnapshot, build_live_safety_snapshot

router = APIRouter()


def _get_live_guard() -> LiveTradingGuard:
    profile = get_profile()
    return LiveTradingGuard(runtime_profile=profile.name)


@router.get("/live-safety", response_model=LiveSafetySnapshot)
async def get_live_safety_snapshot() -> LiveSafetySnapshot:
    guard = _get_live_guard()
    return build_live_safety_snapshot(live_guard=guard)
