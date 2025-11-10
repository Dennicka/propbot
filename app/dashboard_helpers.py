from __future__ import annotations

import logging
import os
import secrets
from typing import Any

from fastapi import Request, status
from fastapi.responses import HTMLResponse

from .secrets_store import SecretsStore
from .security import is_auth_enabled, require_token
from .services.operator_dashboard import build_dashboard_context, render_dashboard_html


LOGGER = logging.getLogger(__name__)


def resolve_operator(request: Request, token: str | None) -> dict[str, str]:
    if not is_auth_enabled():
        return {"name": "local-dev", "role": "operator"}

    operator_name = "unknown"
    operator_role = "viewer"

    if token:
        store: SecretsStore | None
        try:
            store = SecretsStore()
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning(
                "dashboard.resolve_operator.secrets_store_failed",
                extra={"error": str(exc)},
            )
            store = None
        if store:
            resolved = store.get_operator_by_token(token)
            if resolved:
                raw_name, raw_role = resolved
                operator_name = str(raw_name).strip() or "operator"
                normalized_role = str(raw_role or "").strip().lower()
                if normalized_role in {"operator", "auditor", "viewer"}:
                    operator_role = normalized_role
            else:
                operator_name = "token"
        else:
            operator_name = "token"

        expected_token = os.getenv("API_TOKEN")
        if expected_token and secrets.compare_digest(token, expected_token):
            operator_name = "api"
            operator_role = "operator"

    return {"name": operator_name, "role": operator_role}


async def render_dashboard_response(
    request: Request,
    token: str | None,
    *,
    message: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    resolved_token = token
    if resolved_token is None and is_auth_enabled():
        resolved_token = require_token(request)
    context: dict[str, Any] = await build_dashboard_context(request)
    context["operator"] = resolve_operator(request, resolved_token)
    if message:
        context.setdefault("flash_messages", []).append(message)
    html = render_dashboard_html(context)
    return HTMLResponse(content=html, status_code=status_code)


__all__ = ["resolve_operator", "render_dashboard_response"]
