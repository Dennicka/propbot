from __future__ import annotations
from fastapi import APIRouter

router = APIRouter()


@router.get("/pnl")
def pnl() -> dict:
    return {"realized": 0.0, "unrealized": 0.0}
