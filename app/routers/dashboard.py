from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..security import require_token
from ..services.operator_dashboard import build_dashboard_context, render_dashboard_html


router = APIRouter()

async def _require_token(request: Request) -> None:
    require_token(request)


@router.get("/ui/dashboard", response_class=HTMLResponse)
async def operator_dashboard(
    request: Request, _auth: None = Depends(_require_token)
) -> HTMLResponse:
    context: dict[str, Any] = await build_dashboard_context(request)
    html = render_dashboard_html(context)
    return HTMLResponse(content=html)

