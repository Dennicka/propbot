from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..audit_log import log_operator_action
from ..incident.snapshots import (
    CRITICAL_ACTION_INCIDENT_ROLLBACK,
    INCIDENT_MODE_ENABLED,
    load_snapshot,
    save_snapshot,
)
from ..security import require_token
from ..services import approvals_store
from ..utils.operators import resolve_operator_identity

router = APIRouter(prefix="/api/ui/incident", tags=["ui"])


class SnapshotRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class SnapshotResponse(BaseModel):
    path: str


class RollbackRequest(BaseModel):
    path: str
    confirm: bool = Field(default=False)
    request_id: str | None = Field(default=None)


class RollbackPendingResponse(BaseModel):
    status: str
    request: dict[str, Any]


class RollbackAppliedResponse(BaseModel):
    status: str
    request: dict[str, Any]
    snapshot: dict[str, Any]


def _ensure_enabled() -> None:
    if not INCIDENT_MODE_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")


@router.post("/snapshot", response_model=SnapshotResponse)
def snapshot_endpoint(request: Request, payload: SnapshotRequest) -> SnapshotResponse:
    _ensure_enabled()
    token = require_token(request)
    try:
        path = save_snapshot(note=payload.note, token=token)
    except RuntimeError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail
        ) from exc
    identity = resolve_operator_identity(token or "") if token else None
    operator, role = identity if identity else ("unknown", "operator")
    log_operator_action(operator, role, "INCIDENT_SNAPSHOT_REQUEST", details={"path": str(path)})
    return SnapshotResponse(path=str(path))


@router.post("/rollback", response_model=RollbackAppliedResponse | RollbackPendingResponse)
def rollback_endpoint(
    request: Request, payload: RollbackRequest
) -> RollbackAppliedResponse | RollbackPendingResponse:
    _ensure_enabled()
    token = require_token(request)
    identity = resolve_operator_identity(token or "") if token else None
    operator, role = identity if identity else ("unknown", "operator")
    path = payload.path.strip()
    if not path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path_required")
    if not payload.confirm:
        record = approvals_store.create_request(
            CRITICAL_ACTION_INCIDENT_ROLLBACK,
            requested_by=operator,
            parameters={"path": path},
        )
        log_operator_action(
            operator,
            role,
            "INCIDENT_ROLLBACK_REQUEST",
            details={"path": path, "request_id": record.get("id")},
        )
        return RollbackPendingResponse(status="pending", request=record)
    request_id = payload.request_id
    if not request_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="approval_required")
    record = approvals_store.get_request(request_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval_not_found")
    if record.get("action") != CRITICAL_ACTION_INCIDENT_ROLLBACK:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="approval_action_mismatch"
        )
    if str(record.get("status")) != "approved":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="approval_not_complete")
    parameters = record.get("parameters") if isinstance(record, dict) else {}
    approved_path = str(parameters.get("path") or path)
    try:
        approved_resolved = Path(approved_path).resolve()
        requested_resolved = Path(path).resolve()
    except OSError:
        approved_resolved = Path(approved_path)
        requested_resolved = Path(path)
    if approved_resolved != requested_resolved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="approval_path_mismatch"
        )
    try:
        snapshot = load_snapshot(path)
    except RuntimeError as exc:
        detail = str(exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail) from exc
    log_operator_action(
        operator,
        role,
        "INCIDENT_ROLLBACK_APPLY",
        details={"path": path, "request_id": record.get("id")},
    )
    return RollbackAppliedResponse(status="applied", request=record, snapshot=snapshot)
