from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..services.runtime import get_state

router = APIRouter()


@router.post("/flatten")
def hedge_flatten() -> dict:
    state = get_state()
    if not state.derivatives:
        raise HTTPException(status_code=404, detail="derivatives disabled")
    result = state.derivatives.flatten_all()
    return {"ok": True, "result": result}
