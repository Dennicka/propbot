from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.approvals.live_toggle import (
    LiveToggleAction,
    LiveToggleRequest,
    LiveToggleStatus,
    get_live_toggle_store,
)
from app.security import is_auth_enabled, require_token
from app.utils.operators import resolve_operator_identity


router = APIRouter(prefix="/api/ops/live-toggle", tags=["ops"])


@dataclass(slots=True)
class CurrentUser:
    id: str
    role: str


def get_current_user(request: Request) -> CurrentUser:
    if not is_auth_enabled():
        return CurrentUser(id="anonymous", role="operator")
    token = require_token(request)
    if token is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    identity = resolve_operator_identity(token)
    if not identity:
        raise HTTPException(status_code=403, detail="forbidden")
    name, role = identity
    if role != "operator":
        raise HTTPException(status_code=403, detail="forbidden")
    return CurrentUser(id=name, role=role)


class LiveToggleRequestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: LiveToggleAction
    reason: str | None = None


class LiveToggleDecisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_reason: str | None = None


class LiveToggleRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    created_at: datetime
    updated_at: datetime
    action: LiveToggleAction
    requestor_id: str
    approver_id: str | None
    status: LiveToggleStatus
    reason: str | None
    resolution_reason: str | None

    @classmethod
    def from_request(cls, request: LiveToggleRequest) -> "LiveToggleRequestOut":
        return cls.model_validate(request)


@router.post("/requests", response_model=LiveToggleRequestOut)
async def create_live_toggle_request(
    payload: LiveToggleRequestCreate,
    user: CurrentUser = Depends(get_current_user),
) -> LiveToggleRequestOut:
    store = get_live_toggle_store()
    request = store.create_request(
        action=payload.action,
        requestor_id=user.id,
        reason=payload.reason,
    )
    return LiveToggleRequestOut.from_request(request)


@router.post("/requests/{request_id}/approve", response_model=LiveToggleRequestOut)
async def approve_live_toggle_request(
    request_id: str,
    payload: LiveToggleDecisionPayload,
    user: CurrentUser = Depends(get_current_user),
) -> LiveToggleRequestOut:
    store = get_live_toggle_store()
    try:
        request = store.approve_request(
            request_id=request_id,
            approver_id=user.id,
            resolution_reason=payload.resolution_reason,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return LiveToggleRequestOut.from_request(request)


@router.post("/requests/{request_id}/reject", response_model=LiveToggleRequestOut)
async def reject_live_toggle_request(
    request_id: str,
    payload: LiveToggleDecisionPayload,
    user: CurrentUser = Depends(get_current_user),
) -> LiveToggleRequestOut:
    store = get_live_toggle_store()
    try:
        request = store.reject_request(
            request_id=request_id,
            approver_id=user.id,
            resolution_reason=payload.resolution_reason,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return LiveToggleRequestOut.from_request(request)


@router.get("/requests", response_model=list[LiveToggleRequestOut])
async def list_live_toggle_requests() -> list[LiveToggleRequestOut]:
    store = get_live_toggle_store()
    return [LiveToggleRequestOut.from_request(item) for item in store.list_requests()]


__all__ = [
    "CurrentUser",
    "LiveToggleDecisionPayload",
    "LiveToggleRequestCreate",
    "LiveToggleRequestOut",
    "get_current_user",
    "router",
]
