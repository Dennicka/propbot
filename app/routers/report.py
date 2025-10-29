"""Operator-facing operations report endpoints."""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from ..audit_log import log_operator_action
from ..security import is_auth_enabled, require_token
from ..services.report_snapshot import build_ops_report_snapshot, render_ops_report_csv
from ..utils.operators import OperatorIdentity, resolve_operator_identity

_ALLOWED_ROLES = {"viewer", "operator"}

router = APIRouter(prefix="/api/ui", tags=["ui"])


def _require_viewer_access(request: Request) -> Optional[OperatorIdentity]:
    token = require_token(request)
    if not is_auth_enabled():
        return None
    if token is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    identity = resolve_operator_identity(token)
    if not identity:
        raise HTTPException(status_code=401, detail="unauthorized")
    name, role = identity
    if role not in _ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="forbidden")
    return identity


@router.get("/ops_report")
async def ops_report(request: Request) -> Dict[str, Any]:
    identity = _require_viewer_access(request)
    report = await build_ops_report_snapshot()
    if identity:
        log_operator_action(
            operator_name=identity[0],
            role=identity[1],
            action="REPORT_EXPORT",
            details={"format": "json", "endpoint": "/api/ui/ops_report"},
        )
    return report


@router.get("/ops_report.csv", response_class=PlainTextResponse)
async def ops_report_csv(request: Request) -> PlainTextResponse:
    identity = _require_viewer_access(request)
    report = await build_ops_report_snapshot()
    csv_payload = render_ops_report_csv(report)
    if identity:
        log_operator_action(
            operator_name=identity[0],
            role=identity[1],
            action="REPORT_EXPORT",
            details={"format": "csv", "endpoint": "/api/ui/ops_report.csv"},
        )
    return PlainTextResponse(csv_payload, media_type="text/csv")


__all__ = ["router"]
