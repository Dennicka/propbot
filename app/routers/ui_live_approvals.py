from __future__ import annotations

from fastapi import APIRouter

from app.approvals.live_toggle import get_live_toggle_store
from app.routers.ops_live_toggle import LiveToggleRequestOut


router = APIRouter()


@router.get("/live-approvals", response_model=list[LiveToggleRequestOut])
async def get_live_approvals_snapshot() -> list[LiveToggleRequestOut]:
    store = get_live_toggle_store()
    return [LiveToggleRequestOut.from_request(item) for item in store.list_requests()]


__all__ = ["router"]
