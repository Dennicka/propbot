from __future__ import annotations

from fastapi import APIRouter

from ...readiness import READINESS_AGGREGATOR, collect_readiness_signals

router = APIRouter()


@router.get("/live/readiness")
def get_live_readiness() -> dict[str, object]:
    snapshot = READINESS_AGGREGATOR.snapshot(collect_readiness_signals())
    return snapshot


__all__ = ["router"]
