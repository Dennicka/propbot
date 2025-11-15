from __future__ import annotations

from fastapi import APIRouter, Request

from app.ui.ops_status import OpsStatusSnapshot, build_ops_status_snapshot


router = APIRouter(tags=["ui"])


@router.get("", response_model=OpsStatusSnapshot)
async def get_ops_status(request: Request) -> OpsStatusSnapshot:
    return await build_ops_status_snapshot(app=request.app)


__all__ = ["router", "get_ops_status"]
