"""System status endpoint exposing account health summaries."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...security import require_token
from ...services.status import get_status_overview
from ...health.account_health import get_account_health

router = APIRouter(prefix="/api/ui", tags=["ui"])


@router.get("/system_status")
def system_status(request: Request) -> dict[str, object]:
    """Return the current system status snapshot for the UI."""

    require_token(request)
    snapshot = get_status_overview()
    snapshot["account_health"] = get_account_health()
    return snapshot


__all__ = ["router", "system_status"]
