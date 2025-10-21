from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

class HealthOut(BaseModel):
    status: str
    version: str

@router.get("/healthz", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(status="ok", version="test-bot-mvp")


@router.get("/health", response_model=HealthOut)
def health_alias() -> HealthOut:  # pragma: no cover - backwards compatibility
    return health()
