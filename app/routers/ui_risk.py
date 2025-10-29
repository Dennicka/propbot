from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request

from ..security import require_token
from ..strategy_risk import get_strategy_risk_manager

router = APIRouter(tags=["ui"])


@router.get("/risk_status")
def get_risk_status(request: Request) -> dict[str, object]:
    require_token(request)
    manager = get_strategy_risk_manager()
    snapshot = manager.full_snapshot()
    timestamp = snapshot.get("timestamp")
    if not isinstance(timestamp, str):
        snapshot["timestamp"] = datetime.now(timezone.utc).isoformat()
    return snapshot
