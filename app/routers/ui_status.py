from __future__ import annotations
import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

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


@router.websocket("/stream/status")
async def stream_status(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            await ws.send_text(json.dumps(get_status_overview()))
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
