from __future__ import annotations
from fastapi import APIRouter

router = APIRouter()

@router.get("/universe")
def universe() -> dict:
    # paper: пустой универс
    return {"symbols": [], "venues": []}
