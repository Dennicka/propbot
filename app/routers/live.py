from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from ..services.live_readiness import compute_readiness


router = APIRouter()


@router.get("/live-readiness")
def live_readiness() -> JSONResponse:
    payload = compute_readiness()
    http_status = status.HTTP_200_OK if payload.get("ok") else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(content=payload, status_code=http_status)
