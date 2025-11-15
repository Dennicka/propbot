from __future__ import annotations
from fastapi import APIRouter

router = APIRouter()


@router.get("/exposure")
def exposure() -> dict:
    """Return the latest exposure payload consumed by the operator UI."""

    return {"per_venue": {}, "total": 0.0}
