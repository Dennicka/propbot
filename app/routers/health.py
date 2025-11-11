from __future__ import annotations
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..services.health_state import evaluate_health

router = APIRouter()


class HealthOut(BaseModel):
    ok: bool
    journal_ok: bool
    resume_ok: bool
    leader: bool
    config_ok: bool


@router.get("/healthz", response_model=HealthOut, include_in_schema=False)
def health(request: Request):
    snapshot = evaluate_health(request.app)
    payload = HealthOut(
        ok=bool(snapshot.get("ok")),
        journal_ok=bool(snapshot.get("journal_ok", True)),
        resume_ok=bool(snapshot.get("resume_ok", True)),
        leader=bool(snapshot.get("leader", True)),
        config_ok=bool(snapshot.get("config_ok", True)),
    )
    if payload.ok:
        return payload
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=payload.model_dump(),
    )


@router.get("/health", response_model=HealthOut, include_in_schema=False)
def health_alias(request: Request):  # pragma: no cover - backwards compatibility
    return health(request)
