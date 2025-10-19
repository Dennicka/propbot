from __future__ import annotations
from fastapi import APIRouter
router = APIRouter()

@router.get("/status")
def status() -> dict:
    return {"status": "OK", "mismatch": 0}

@router.post("/run")
def run() -> dict:
    # mock recon pass
    return {"ok": True, "checked": 0, "mismatch": 0}

@router.get("/history")
def history() -> dict:
    return {"items": []}
