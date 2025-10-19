from __future__ import annotations
from fastapi import APIRouter

router = APIRouter()

@router.get("/exposure")
def exposure() -> dict:
    return {"per_venue": {}, "total": 0.0}
