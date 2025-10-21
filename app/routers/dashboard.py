from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "status.html"
_STATUS_HTML = _TEMPLATE_PATH.read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse)
async def status_page() -> HTMLResponse:
    return HTMLResponse(content=_STATUS_HTML)
