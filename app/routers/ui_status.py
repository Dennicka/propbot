from __future__ import annotations
import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services.cache import get_or_set
from ..services.status import get_status_components, get_status_overview, get_status_slo

router = APIRouter()

@router.get("/overview")
async def overview() -> dict:
    return await get_or_set(
        "/api/ui/status/overview",
        1.0,
        get_status_overview,
    )

@router.get("/components")
async def components() -> dict:
    return await get_or_set(
        "/api/ui/status/components",
        1.0,
        get_status_components,
    )

@router.get("/slo")
async def slo() -> dict:
    return await get_or_set(
        "/api/ui/status/slo",
        1.0,
        get_status_slo,
    )


@router.websocket("/stream/status")
async def stream_status(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            await ws.send_text(json.dumps(get_status_overview()))
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
