"""Exchange watchdog API endpoints."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request, status

from ..exchange_watchdog import get_exchange_watchdog
from ..security import is_auth_enabled, require_token
from ..utils.operators import resolve_operator_identity

router = APIRouter(prefix="/exchange_health", tags=["ui"])


async def _ensure_viewer_access(request: Request) -> None:
    if not is_auth_enabled():
        return
    token = require_token(request)
    if not token:
        return
    identity = resolve_operator_identity(token)
    if identity is None:
        # Global API tokens are treated as operator-level access.
        return
    _, role = identity
    if role not in {"viewer", "auditor", "operator"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


@router.get("", name="exchange-health")
async def get_exchange_health(request: Request) -> Dict[str, Dict[str, Any]]:
    await _ensure_viewer_access(request)
    watchdog = get_exchange_watchdog()
    return watchdog.get_state()


__all__ = ["router"]
