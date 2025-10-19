from __future__ import annotations
from fastapi import APIRouter
from ..services.status import get_status_overview, get_status_components, get_status_slo

router = APIRouter()

@router.get("/overview")
def overview() -> dict:
    return get_status_overview()

@router.get("/components")
def components() -> dict:
    return get_status_components()

@router.get("/slo")
def slo() -> dict:
    return get_status_slo()
