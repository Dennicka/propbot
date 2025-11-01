"""UI endpoints for partial hedge planning and execution."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..security import require_token
from ..services.partial_hedge_runner import execute_now, get_partial_hedge_status, refresh_plan

router = APIRouter(prefix="/hedge", tags=["ui"])


class ExecutePayload(BaseModel):
    confirm: bool = Field(True, description="Explicit operator confirmation flag")


@router.get("/plan", name="partial-hedge-plan")
async def get_partial_hedge_plan(request: Request) -> dict[str, Any]:
    require_token(request)
    snapshot = await refresh_plan()
    plan = snapshot.get("plan") if isinstance(snapshot.get("plan"), dict) else {}
    orders = snapshot.get("orders") if isinstance(snapshot.get("orders"), list) else []
    totals = plan.get("totals") if isinstance(plan.get("totals"), dict) else {}
    status_payload = get_partial_hedge_status()
    return {
        "orders": orders,
        "totals": totals,
        "plan": plan,
        "status": snapshot.get("status"),
        "runner": status_payload,
    }


@router.post("/execute", name="partial-hedge-execute")
async def execute_partial_hedge(request: Request, payload: ExecutePayload) -> dict[str, Any]:
    require_token(request)
    if not payload.confirm:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="confirmation_required")
    try:
        snapshot = await execute_now(payload.confirm)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    status_payload = get_partial_hedge_status()
    return {
        "execution": snapshot,
        "runner": status_payload,
    }


__all__ = ["router"]
