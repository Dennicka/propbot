from __future__ import annotations

import hashlib
import os
import secrets
import threading
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from ..dashboard_helpers import render_dashboard_response, resolve_operator
from ..security import require_token
from ..services.cache import get_or_set
from ..services.operator_dashboard import build_dashboard_context, render_dashboard_html
from .ui import (
    dashboard_hold_action,
    dashboard_kill_request_action,
    dashboard_resume_request_action,
)


router = APIRouter()

_ETAG_CACHE: dict[str, datetime] = {}
_ETAG_CACHE_LOCK = threading.Lock()


def _normalise_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_http_dt(dt: datetime) -> str:
    return format_datetime(_normalise_dt(dt))


def _etag_for(html: str) -> str:
    digest = hashlib.sha1(html.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f'W/"{digest}"'


def _etag_headers(etag: str, last_modified: datetime) -> dict[str, str]:
    return {
        "ETag": etag,
        "Last-Modified": _format_http_dt(last_modified),
        "Cache-Control": "no-cache, must-revalidate",
    }


def _if_none_match_matches(header_value: str | None, etag: str) -> bool:
    if not header_value:
        return False
    candidates = [item.strip() for item in header_value.split(",") if item.strip()]
    return "*" in candidates or etag in candidates


def _not_modified_since(header_value: str | None, last_modified: datetime) -> bool:
    if not header_value:
        return False
    try:
        parsed = parsedate_to_datetime(header_value)
    except (TypeError, ValueError):
        return False
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _normalise_dt(last_modified) <= parsed


async def _require_token(request: Request) -> str | None:
    return require_token(request)


@router.get("/ui/dashboard", response_class=HTMLResponse)
async def operator_dashboard(
    request: Request, token: str | None = Depends(_require_token)
) -> HTMLResponse:
    async def _load_context() -> dict[str, Any]:
        return await build_dashboard_context(request)

    base_context = await get_or_set(
        "/ui/dashboard/context",
        1.0,
        _load_context,
    )
    context: dict[str, Any] = dict(base_context)
    context["operator"] = resolve_operator(request, token)
    html = render_dashboard_html(context)
    etag = _etag_for(html)
    with _ETAG_CACHE_LOCK:
        last_modified = _ETAG_CACHE.get(etag)
        if last_modified is None:
            last_modified = datetime.now(timezone.utc)
            _ETAG_CACHE[etag] = last_modified
    headers = _etag_headers(etag, last_modified)
    if _if_none_match_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    if _not_modified_since(request.headers.get("if-modified-since"), last_modified):
        return Response(status_code=304, headers=headers)
    response = HTMLResponse(content=html)
    for header, value in headers.items():
        response.headers[header] = value
    return response


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

