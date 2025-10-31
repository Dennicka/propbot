from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from ..services.live_readiness import compute_readiness


router = APIRouter()


@router.get("/live-readiness")
def live_readiness(request: Request) -> JSONResponse:
    payload = compute_readiness(request.app)
    http_status = (
        status.HTTP_200_OK if payload.get("ready") else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return JSONResponse(content=payload, status_code=http_status)
