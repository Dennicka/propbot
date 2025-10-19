from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

class HealthOut(BaseModel):
    status: str
    version: str

@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(status="ok", version="6.3.2-final")
