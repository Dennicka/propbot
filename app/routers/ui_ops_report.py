"""Ops report API endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..security import is_auth_enabled, require_token
from ..services.ops_report import build_ops_report, build_ops_report_csv
from ..utils.operators import resolve_operator_identity

router = APIRouter(prefix="/ops_report", tags=["ui"])


async def _ensure_viewer(request: Request) -> None:
    if not is_auth_enabled():
        return
    token = require_token(request)
    if not token:
        return
    identity = resolve_operator_identity(token)
    if identity is None:
        # ``require_token`` already enforces token validity, so at this point the
        # caller is authenticated via the global API token. Treat it as
        # operator-level access for read-only routes.
        return


@router.get("", name="ops-report-json")
async def get_ops_report(request: Request) -> dict[str, Any]:
    await _ensure_viewer(request)
    return await build_ops_report()


@router.get(".csv", name="ops-report-csv")
async def get_ops_report_csv(request: Request) -> Response:
    await _ensure_viewer(request)
    report = await build_ops_report()
    csv_body = build_ops_report_csv(report)
    return Response(content=csv_body, media_type="text/csv")


__all__ = ["router"]
