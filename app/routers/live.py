from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

class LiveOut(BaseModel):
    s: str

@router.get("/live-readiness", response_model=LiveOut)
def live_readiness() -> LiveOut:
    # In paper mode always ready
    return LiveOut(s="READY")
