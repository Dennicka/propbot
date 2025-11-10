"""System status endpoint exposing account health summaries."""

from __future__ import annotations

from typing import Mapping

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
    recon_payload = snapshot.get("recon")
    if isinstance(recon_payload, Mapping):
        recon_copy = dict(recon_payload)
        recon_copy["enabled"] = bool(recon_payload.get("enabled", True))
        last_run_ts = recon_payload.get("last_run_ts")
        recon_copy["last_run_ts"] = last_run_ts
        last_severity = recon_payload.get("last_severity") or recon_payload.get("status")
        if isinstance(last_severity, str):
            last_severity = last_severity.upper()
        recon_copy["last_severity"] = last_severity
        snapshot["recon"] = recon_copy
    snapshot["account_health"] = get_account_health()
    return snapshot


__all__ = ["router", "system_status"]
