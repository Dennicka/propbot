from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse

from ..security import require_token
from ..services.operator_dashboard import build_dashboard_context, render_dashboard_html
from .ui import (
    HoldPayload,
    ResumeRequestPayload,
    hold as hold_action,
    kill_switch as kill_action,
    resume_request as resume_request_action,
)


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


async def _render_dashboard_response(
    request: Request,
    *,
    message: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    context: dict[str, Any] = await build_dashboard_context(request)
    if message:
        context.setdefault("flash_messages", []).append(message)
    html = render_dashboard_html(context)
    return HTMLResponse(content=html, status_code=status_code)


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
    _auth: None = Depends(_require_token),
) -> HTMLResponse:
    form_data = _parse_form_payload(await request.body())
    reason = form_data.get("reason", "")
    operator = form_data.get("operator", "")
    payload = HoldPayload(reason=reason or None, requested_by=operator or "dashboard_ui")
    result = await hold_action(request, payload)
    hold_reason = result.get("safety", {}).get("hold_reason") or payload.reason or "manual_hold"
    message = f"HOLD engaged — reason: {hold_reason}"
    return await _render_dashboard_response(request, message=message)


@router.post("/ui/dashboard/resume", response_class=HTMLResponse)
async def dashboard_resume_request(
    request: Request,
    _auth: None = Depends(_require_token),
) -> HTMLResponse:
    form_data = _parse_form_payload(await request.body())
    reason = form_data.get("reason", "")
    operator = form_data.get("operator", "")
    payload = ResumeRequestPayload(reason=reason, requested_by=operator or "dashboard_ui")
    result = await resume_request_action(request, payload)
    request_id = result.get("resume_request", {}).get("id")
    message = "Resume request logged"
    if request_id:
        message += f" (approval id: {request_id})"
    return await _render_dashboard_response(
        request,
        message=message + "; awaiting second-operator approval.",
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.post("/ui/dashboard/kill", response_class=HTMLResponse)
async def dashboard_kill(
    request: Request,
    _auth: None = Depends(_require_token),
) -> HTMLResponse:
    form_data = _parse_form_payload(await request.body())
    operator = form_data.get("operator", "")
    await kill_action(request)
    operator_label = operator or "dashboard_ui"
    message = f"Kill switch engaged by {operator_label}"
    return await _render_dashboard_response(request, message=message)

