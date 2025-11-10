from __future__ import annotations
import asyncio
import json

from typing import Any, Mapping

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from ..services import runtime
from ..services import cache as status_cache
from ..services.cache import get_or_set
from ..services.status import get_status_components, get_status_overview, get_status_slo
from ..slo.guard import apply_critical_slo_auto_hold, build_default_context
from ..telemetry.metrics import slo_snapshot
from ..utils.ttl_cache import cache_response

router = APIRouter()

_OVERVIEW_CACHE_KEY = "/api/ui/status/overview"
_OVERVIEW_TTL = 1.0
_SEVERITY_RANK = {"OK": 0, "WARN": 1, "ERROR": 2, "HOLD": 3}


def _cache_enabled() -> bool:
    try:
        return status_cache._cache_enabled()  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - defensive
        return True


def _refresh_overview_cache(payload: Mapping[str, object]) -> None:
    if not _cache_enabled():
        return
    ttl = max(status_cache._default_ttl(_OVERVIEW_TTL), 0.0)  # type: ignore[attr-defined]
    expires_at = status_cache._monotonic() + ttl  # type: ignore[attr-defined]
    status_cache._STORE.set(_OVERVIEW_CACHE_KEY, payload, expires_at)  # type: ignore[attr-defined]


def _recompute_overall(snapshot: dict[str, Any], hold_active: bool) -> None:
    if hold_active:
        snapshot["overall"] = "HOLD"
        return
    components = snapshot.get("components")
    if not isinstance(components, list):
        snapshot.setdefault("overall", "OK")
        return
    best = "OK"
    for entry in components:
        if not isinstance(entry, Mapping):
            continue
        status = str(entry.get("status") or "").upper()
        if _SEVERITY_RANK.get(status, -1) > _SEVERITY_RANK.get(best, -1):
            best = status
    if best == "HOLD":
        fallback = "OK"
        for entry in components:
            if not isinstance(entry, Mapping):
                continue
            status = str(entry.get("status") or "").upper()
            if status == "HOLD":
                continue
            if _SEVERITY_RANK.get(status, -1) > _SEVERITY_RANK.get(fallback, -1):
                fallback = status
        best = fallback
    snapshot["overall"] = best


@router.get("/overview")
@cache_response(ttl_s=_OVERVIEW_TTL, allow_in_tests=True, refresh_on_hit=True)
async def overview(_request: Request) -> JSONResponse:
    cached = await get_or_set(
        _OVERVIEW_CACHE_KEY,
        _OVERVIEW_TTL,
        get_status_overview,
        allow_in_tests=True,
    )
    context = build_default_context()
    context.runtime = runtime
    context.state = runtime.get_state()
    slo_data = slo_snapshot()
    guard_reason = apply_critical_slo_auto_hold(context, slo_data)

    state = runtime.get_state()
    safety_payload = state.safety.status_payload()
    hold_reason = safety_payload.get("hold_reason")
    hold_active = bool(safety_payload.get("hold_active", False))

    needs_refresh = False
    cached_safety = cached.get("safety") if isinstance(cached, Mapping) else None
    if hold_active:
        if not isinstance(cached_safety, Mapping):
            needs_refresh = True
        else:
            cached_hold_active = bool(cached_safety.get("hold_active", False))
            if not cached_hold_active:
                needs_refresh = True
            elif cached_safety.get("hold_reason") != hold_reason:
                needs_refresh = True
    else:
        if isinstance(cached_safety, Mapping) and bool(cached_safety.get("hold_active", False)):
            needs_refresh = True

    if isinstance(cached, Mapping):
        snapshot: dict[str, Any] = dict(cached)
    else:
        snapshot = {}

    cached_hold_since = None
    if isinstance(cached_safety, Mapping):
        cached_hold_since = cached_safety.get("hold_since")

    if hold_active:
        snapshot["hold_active"] = True
        snapshot["hold_reason"] = hold_reason
        snapshot["hold_source"] = safety_payload.get("hold_source")
        safety_section = dict(safety_payload)
        if cached_hold_since is not None and safety_section.get("hold_since") is not None:
            safety_section["hold_since"] = cached_hold_since
        snapshot["safety"] = safety_section
        snapshot["mode"] = state.control.mode
        snapshot["safe_mode"] = state.control.safe_mode
    else:
        snapshot["hold_active"] = False
        snapshot.pop("hold_reason", None)
        snapshot.pop("hold_source", None)
        safety_section = snapshot.get("safety")
        if isinstance(safety_section, Mapping):
            cleared = dict(safety_section)
            cleared["hold_active"] = False
            cleared["hold_reason"] = None
            cleared["hold_source"] = None
            snapshot["safety"] = cleared
        snapshot["mode"] = state.control.mode
        snapshot["safe_mode"] = state.control.safe_mode

    if needs_refresh:
        _refresh_overview_cache(snapshot)

    auto_hold_reason: str | None = None
    if guard_reason:
        auto_hold_reason = guard_reason
    elif hold_active:
        maybe_reason = safety_payload.get("hold_reason")
        if isinstance(maybe_reason, str) and maybe_reason.startswith("SLO_CRITICAL::"):
            auto_hold_reason = maybe_reason
    if auto_hold_reason is not None:
        snapshot["auto_hold_reason"] = auto_hold_reason
    else:
        snapshot.pop("auto_hold_reason", None)

    _recompute_overall(snapshot, hold_active)

    response = JSONResponse(content=snapshot)
    response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
    return response


@router.get("/components")
@cache_response(ttl_s=1.0, allow_in_tests=True)
async def components(_request: Request) -> JSONResponse:
    payload = await get_or_set(
        "/api/ui/status/components",
        1.0,
        get_status_components,
        allow_in_tests=True,
    )
    response = JSONResponse(content=payload)
    response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
    return response


@router.get("/slo")
@cache_response(ttl_s=1.0, allow_in_tests=True)
async def slo(_request: Request) -> JSONResponse:
    payload = await get_or_set(
        "/api/ui/status/slo",
        1.0,
        get_status_slo,
        allow_in_tests=True,
    )
    response = JSONResponse(content=payload)
    response.headers.setdefault("Cache-Control", "no-cache, must-revalidate")
    return response


@router.websocket("/stream/status")
async def stream_status(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            await ws.send_text(json.dumps(get_status_overview()))
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
