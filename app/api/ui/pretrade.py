"""UI API endpoints exposing the pre-trade gate status."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...security import require_token
from ...services import runtime


router = APIRouter(prefix="/api/ui", tags=["ui"])


def _snapshot() -> dict[str, object | None]:
    snapshot = runtime.get_pre_trade_gate_status()
    throttled = bool(snapshot.get("throttled"))
    reason = snapshot.get("reason")
    payload: dict[str, object | None] = {"throttled": throttled, "reason": reason}
    updated_ts = snapshot.get("updated_ts")
    if updated_ts is not None:
        payload["updated_ts"] = updated_ts
    return payload


@router.get("/pretrade_gate")
def pretrade_gate_status(request: Request) -> dict[str, object | None]:
    """Return the current throttle state for the pre-trade gate."""

    require_token(request)
    return _snapshot()


@router.get("/pretrade/status")
def pretrade_status(request: Request) -> dict[str, object | None]:
    """Compatibility alias for clients polling the pre-trade gate."""

    require_token(request)
    return _snapshot()


def get_pretrade_gate_status() -> dict[str, object | None]:
    """Expose the pre-trade gate snapshot for other modules/tests."""

    return _snapshot()


def get_pretrade_status() -> dict[str, object | None]:
    """Alias mirroring the HTTP contract for compatibility."""

    return _snapshot()


__all__ = [
    "router",
    "pretrade_gate_status",
    "pretrade_status",
    "get_pretrade_gate_status",
    "get_pretrade_status",
]
