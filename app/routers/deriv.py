from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services.runtime import get_state

router = APIRouter()


class SetupRequest(BaseModel):
    venues: dict[str, dict[str, object]]


@router.get("/status")
def deriv_status() -> dict:
    state = get_state()
    if not state.derivatives:
        raise HTTPException(status_code=404, detail="derivatives disabled")
    return state.derivatives.status_payload()


@router.post("/setup")
def deriv_setup(body: SetupRequest) -> dict:
    state = get_state()
    if not state.derivatives:
        raise HTTPException(status_code=404, detail="derivatives disabled")
    return state.derivatives.set_modes(body.venues)


@router.get("/positions")
def deriv_positions() -> dict:
    state = get_state()
    if not state.derivatives:
        raise HTTPException(status_code=404, detail="derivatives disabled")
    return state.derivatives.positions_payload()
