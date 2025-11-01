from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request

from ..security import require_token
from ..services.cache import get_or_set
from ..services.pnl_attribution import build_pnl_attribution

router = APIRouter()


@router.get("/pnl_attrib")
async def get_pnl_attribution(request: Request) -> Dict[str, Any]:
    require_token(request)

    async def _load() -> Dict[str, Any]:
        return await build_pnl_attribution()

    return await get_or_set(
        "/api/ui/pnl_attrib",
        2.0,
        _load,
    )


__all__ = ["router"]
