from __future__ import annotations

from fastapi import APIRouter

from ..services import risk

router = APIRouter(prefix="/api/risk", tags=["risk"])


@router.get("/state")
async def risk_state() -> dict:
    return risk.risk_overview()
