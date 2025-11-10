from __future__ import annotations
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from ..services.status import get_status_overview
import asyncio
import json

router = APIRouter()

@router.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            await ws.send_text(json.dumps(get_status_overview()))
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
