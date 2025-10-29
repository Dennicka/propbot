from __future__ import annotations

import os
import secrets
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from ..dashboard_helpers import render_dashboard_response, resolve_operator
from ..security import require_token
from ..services.operator_dashboard import build_dashboard_context, render_dashboard_html
from .ui import (
    dashboard_hold_action,
    dashboard_kill_request_action,
    dashboard_resume_request_action,
)


router = APIRouter()


async def _require_token(request: Request) -> str | None:
    return require_token(request)


@router.get("/ui/dashboard", response_class=HTMLResponse)
async def operator_dashboard(
    request: Request, token: str | None = Depends(_require_token)
) -> HTMLResponse:
    context: dict[str, Any] = await build_dashboard_context(request)
    context["operator"] = resolve_operator(request, token)
    html = render_dashboard_html(context)
    return HTMLResponse(content=html)


def _parse_form_payload(raw: bytes) -> dict[str, str]:
    if not raw:
        return {}
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = raw.decode("latin1", errors="ignore")
    parsed = parse_qs(decoded, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


@router.post("/ui/dashboard/hold", response_class=HTMLResponse)
async def dashboard_hold(
    request: Request,
    token: str | None = Depends(_require_token),
) -> HTMLResponse:
    form_data = _parse_form_payload(await request.body())
    return await dashboard_hold_action(
        request,
        reason=form_data.get("reason", ""),
        operator=form_data.get("operator", ""),
        token=token,
    )


@router.post("/ui/dashboard/resume", response_class=HTMLResponse)
async def dashboard_resume_request(
    request: Request,
    token: str | None = Depends(_require_token),
) -> HTMLResponse:
    form_data = _parse_form_payload(await request.body())
    return await dashboard_resume_request_action(
        request,
        reason=form_data.get("reason", ""),
        operator=form_data.get("operator", ""),
        token=token,
    )


@router.post("/ui/dashboard/kill", response_class=HTMLResponse)
async def dashboard_kill(
    request: Request,
    token: str | None = Depends(_require_token),
) -> HTMLResponse:
    form_data = _parse_form_payload(await request.body())
    operator = form_data.get("operator", "")
    reason = form_data.get("reason", "")
    return await dashboard_kill_request_action(
        request,
        token=token,
        operator=operator,
        reason=reason,
    )

