from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..services.runtime import get_safety_status, get_state, is_hold_active

router = APIRouter()


@router.post("/flatten")
def hedge_flatten() -> dict:
    if is_hold_active():
        safety = get_safety_status()
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail={"error": "hold_active", "reason": safety.get("hold_reason")},
        )
    state = get_state()
    if not state.derivatives:
        raise HTTPException(status_code=404, detail="derivatives disabled")
    result = state.derivatives.flatten_all()
    return {"ok": True, "result": result}
