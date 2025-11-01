from __future__ import annotations

import os
from typing import Any, Dict

from fastapi import APIRouter, Request

from ..security import require_token
from ..utils.ttl_cache import cache_response
from ..services.pnl_attribution import build_pnl_attribution

router = APIRouter()


def _auth_cache_vary(request: Request, _args, _kwargs) -> tuple[str, ...]:
    auth = request.headers.get("authorization", "")
    marker = os.getenv("PYTEST_CURRENT_TEST", "")
    parts = []
    if auth:
        parts.append(auth)
    if marker:
        parts.append(marker)
    return tuple(parts)


@router.get("/pnl_attrib")
@cache_response(2.0, vary=_auth_cache_vary)
async def get_pnl_attribution(request: Request) -> Dict[str, Any]:
    require_token(request)
    return await build_pnl_attribution()


__all__ = ["router"]
