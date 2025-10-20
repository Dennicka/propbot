from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel

from ..services.runtime import get_state

router = APIRouter()


class LiveOut(BaseModel):
    s: str
    safe_mode: bool
    preflight: bool


@router.get("/live-readiness", response_model=LiveOut)
def live_readiness() -> LiveOut:
    state = get_state()
    if state.control.safe_mode:
        status = "READY"
    else:
        ready = state.control.preflight_passed and len(state.control.approvals) >= 2
        status = "READY" if ready else "HOLD"
    return LiveOut(s=status, safe_mode=state.control.safe_mode, preflight=state.control.preflight_passed)
