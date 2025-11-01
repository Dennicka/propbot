from __future__ import annotations
import asyncio
import json

from typing import Any, Mapping

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..services import cache as status_cache
from ..services.cache import get_or_set
from ..services.status import get_status_components, get_status_overview, get_status_slo
from ..services import runtime
from ..slo.guard import apply_critical_slo_auto_hold, build_default_context
from ..telemetry.metrics import slo_snapshot

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
async def overview() -> dict:
    cached = await get_or_set(
        _OVERVIEW_CACHE_KEY,
        _OVERVIEW_TTL,
        get_status_overview,
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
    if not isinstance(cached_safety, Mapping):
        needs_refresh = True
    else:
        if bool(cached_safety.get("hold_active", False)) != hold_active:
            needs_refresh = True
        elif cached_safety.get("hold_reason") != hold_reason:
            needs_refresh = True

    if needs_refresh:
        fresh = get_status_overview()
        _refresh_overview_cache(fresh)
        snapshot = dict(fresh)
    else:
        snapshot = dict(cached)

    if hold_active and isinstance(snapshot, dict):
        snapshot["hold_active"] = True
        snapshot["hold_reason"] = hold_reason
        snapshot["hold_source"] = safety_payload.get("hold_source")
        snapshot["safety"] = dict(safety_payload)
        snapshot["mode"] = state.control.mode
        snapshot["safe_mode"] = state.control.safe_mode
    elif not hold_active and needs_refresh:
        snapshot["hold_active"] = False

    if guard_reason:
        snapshot["auto_hold_reason"] = guard_reason

    _recompute_overall(snapshot, hold_active)

    return snapshot

@router.get("/components")
async def components() -> dict:
    return await get_or_set(
        "/api/ui/status/components",
        1.0,
        get_status_components,
    )

@router.get("/slo")
async def slo() -> dict:
    return await get_or_set(
        "/api/ui/status/slo",
        1.0,
        get_status_slo,
    )


@router.websocket("/stream/status")
async def stream_status(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            await ws.send_text(json.dumps(get_status_overview()))
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
