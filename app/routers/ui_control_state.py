from __future__ import annotations
from fastapi import APIRouter

router = APIRouter()

@router.get("/control-state")
def control_state() -> dict:
    return {"mode": "HOLD", "two_man_rule": True}
