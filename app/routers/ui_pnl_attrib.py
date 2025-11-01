from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request

from ..security import require_token
from ..services.pnl_attribution import build_pnl_attribution

router = APIRouter()


@router.get("/pnl_attrib")
async def get_pnl_attribution(request: Request) -> Dict[str, Any]:
    require_token(request)
    return await build_pnl_attribution()


__all__ = ["router"]
