"""In-memory live toggle approval store with two-man rule enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Dict, Literal, Optional
from uuid import uuid4


LiveToggleAction = Literal["enable_live", "disable_live"]
LiveToggleStatus = Literal["pending", "approved", "rejected", "cancelled"]


@dataclass(slots=True)
class LiveToggleRequest:
    id: str
    created_at: datetime
    updated_at: datetime
    action: LiveToggleAction

    requestor_id: str
    approver_id: Optional[str]

    status: LiveToggleStatus
    reason: Optional[str]
    resolution_reason: Optional[str]


@dataclass(slots=True)
class LiveToggleEffectiveState:
    """Aggregated state for live approvals."""

    enabled: bool
    last_action: LiveToggleAction | None
    last_status: LiveToggleStatus | None
    last_updated_at: Optional[datetime]
    last_request_id: Optional[str]
    requestor_id: Optional[str]
    approver_id: Optional[str]
    resolution_reason: Optional[str]


class LiveToggleStore:
    """Простой in-memory store для заявок на live-тоггл (two-man rule)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: Dict[str, LiveToggleRequest] = {}

    def create_request(
        self,
        *,
        action: LiveToggleAction,
        requestor_id: str,
        reason: str | None = None,
    ) -> LiveToggleRequest:
        now = datetime.now(timezone.utc)
        request = LiveToggleRequest(
            id=uuid4().hex,
            created_at=now,
            updated_at=now,
            action=action,
            requestor_id=requestor_id,
            approver_id=None,
            status="pending",
            reason=reason,
            resolution_reason=None,
        )
        with self._lock:
            self._requests[request.id] = request
        return request

    def list_requests(self) -> list[LiveToggleRequest]:
        with self._lock:
            # Return requests sorted by creation time for deterministic output
            return sorted(self._requests.values(), key=lambda item: item.created_at)

    def get_request(self, request_id: str) -> LiveToggleRequest | None:
        with self._lock:
            return self._requests.get(request_id)

    def approve_request(
        self,
        *,
        request_id: str,
        approver_id: str,
        resolution_reason: str | None = None,
    ) -> LiveToggleRequest:
        return self._resolve_request(
            request_id=request_id,
            approver_id=approver_id,
            status="approved",
            resolution_reason=resolution_reason,
        )

    def reject_request(
        self,
        *,
        request_id: str,
        approver_id: str,
        resolution_reason: str | None = None,
    ) -> LiveToggleRequest:
        return self._resolve_request(
            request_id=request_id,
            approver_id=approver_id,
            status="rejected",
            resolution_reason=resolution_reason,
        )

    def _resolve_request(
        self,
        *,
        request_id: str,
        approver_id: str,
        status: Literal["approved", "rejected"],
        resolution_reason: str | None,
    ) -> LiveToggleRequest:
        with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise ValueError(f"live toggle request not found: {request_id}")
            if request.requestor_id == approver_id:
                raise PermissionError("two-man rule violated: approver matches requestor")
            if request.status != "pending":
                raise RuntimeError(
                    f"cannot resolve request with status={request.status}; expected 'pending'"
                )
            now = datetime.now(timezone.utc)
            request.status = status
            request.approver_id = approver_id
            request.resolution_reason = resolution_reason
            request.updated_at = now
            return request

    def get_effective_state(self) -> LiveToggleEffectiveState:
        with self._lock:
            if not self._requests:
                return LiveToggleEffectiveState(
                    enabled=False,
                    last_action=None,
                    last_status=None,
                    last_updated_at=None,
                    last_request_id=None,
                    requestor_id=None,
                    approver_id=None,
                    resolution_reason=None,
                )
            latest_request = max(self._requests.values(), key=lambda item: item.updated_at)

        enabled = latest_request.status == "approved" and latest_request.action == "enable_live"
        return LiveToggleEffectiveState(
            enabled=enabled,
            last_action=latest_request.action,
            last_status=latest_request.status,
            last_updated_at=latest_request.updated_at,
            last_request_id=latest_request.id,
            requestor_id=latest_request.requestor_id,
            approver_id=latest_request.approver_id,
            resolution_reason=latest_request.resolution_reason,
        )


_live_toggle_store = LiveToggleStore()


def get_live_toggle_store() -> LiveToggleStore:
    return _live_toggle_store


__all__ = [
    "LiveToggleAction",
    "LiveToggleEffectiveState",
    "LiveToggleRequest",
    "LiveToggleStatus",
    "LiveToggleStore",
    "get_live_toggle_store",
]
