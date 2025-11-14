from __future__ import annotations

from dataclasses import dataclass
import os
import sys
from functools import wraps
from time import time
from typing import Any, Dict, Mapping, Optional, Set

from fastapi import Request, status
from fastapi.responses import JSONResponse

from app.alerts.events import evt_readiness
from app.health.watchdog import HealthSnapshot, get_watchdog
from app.metrics.core import (
    HEALTH_WATCHDOG_COMPONENT_GAUGE,
    HEALTH_WATCHDOG_OVERALL_GAUGE,
)
from app.ops.hooks import ops_alert
from app.services.runtime import get_runtime_profile_snapshot

DEFAULT_REQUIRED_SIGNALS: Set[str] = {"market", "recon", "adapters"}


_LEVELS: tuple[str, ...] = ("ok", "warn", "fail")
_PATCHED = False
_APP_PATCHED = False
_ORIGINAL_COMPUTE_READINESS: Any | None = None
_ORIGINAL_HEALTH_ENDPOINT: Any | None = None


@dataclass
class Signal:
    ok: bool
    reason: str = ""
    ts: float = 0.0


class HealthAggregator:
    def __init__(self, ttl_seconds: int = 30, required: Optional[Set[str]] = None) -> None:
        self._ttl = int(ttl_seconds)
        self._req: Set[str] = set(required) if required else set(DEFAULT_REQUIRED_SIGNALS)
        self._signals: Dict[str, Signal] = {}
        self._last_ready: Optional[bool] = None

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    @property
    def required(self) -> Set[str]:
        return set(self._req)

    def configure(
        self,
        *,
        ttl_seconds: Optional[int] = None,
        required: Optional[Set[str]] = None,
    ) -> None:
        if ttl_seconds is not None:
            self._ttl = int(ttl_seconds)
        if required is not None:
            self._req = set(required) if required else set(DEFAULT_REQUIRED_SIGNALS)

    def set(self, name: str, ok: bool, *, reason: str = "", now: Optional[float] = None) -> None:
        self._signals[name] = Signal(ok=ok, reason=reason, ts=(now or time()))

    def get(self, name: str) -> Optional[Signal]:
        return self._signals.get(name)

    def clear(self) -> None:
        self._signals.clear()
        self._last_ready = None

    def is_ready(self, now: Optional[float] = None) -> tuple[bool, str]:
        t = now or time()
        missing = []
        bad = []
        for name in sorted(self._req):
            signal = self._signals.get(name)
            if not signal or (t - signal.ts) > self._ttl:
                missing.append(name)
            elif not signal.ok:
                bad.append(f"{name}:{signal.reason or 'fail'}")
        if missing:
            ready = False
            detail = "readiness-missing:" + ",".join(missing)
        elif bad:
            ready = False
            detail = "readiness-bad:" + ",".join(bad)
        else:
            ready = True
            detail = "ok"
        previous = self._last_ready
        self._last_ready = ready
        if previous is True and not ready:
            ops_alert(evt_readiness("bad", detail))
        elif previous is False and ready:
            ops_alert(evt_readiness("ok", "recovered"))
        return ready, detail


_AGG = HealthAggregator()


def get_agg() -> HealthAggregator:
    return _AGG


def _watchdog_payload(snapshot: HealthSnapshot) -> dict[str, Any]:
    components: dict[str, dict[str, Any]] = {}
    for name, component in snapshot.components.items():
        components[name] = {
            "level": component.level,
            "reason": component.reason,
            "last_ts": component.last_ts,
        }
    return {
        "overall": snapshot.overall,
        "components": components,
        "ts": snapshot.ts,
    }


def _update_watchdog_metrics(snapshot: HealthSnapshot) -> None:
    for level in _LEVELS:
        value = 1.0 if snapshot.overall == level else 0.0
        HEALTH_WATCHDOG_OVERALL_GAUGE.labels(level=level).set(value)
    for name, component in snapshot.components.items():
        for level in _LEVELS:
            value = 1.0 if component.level == level else 0.0
            HEALTH_WATCHDOG_COMPONENT_GAUGE.labels(component=name, level=level).set(value)


def _append_watchdog_to_readiness(
    payload: Dict[str, Any], snapshot: HealthSnapshot
) -> Dict[str, Any]:
    result = dict(payload)
    reasons = [str(reason) for reason in result.get("reasons", []) if str(reason)]
    ready = bool(result.get("ready", False))
    runtime = get_runtime_profile_snapshot()
    strict_profile = os.environ.get("HEALTH_PROFILE_STRICT", "live")
    fail_on_warn = os.environ.get("HEALTH_FAIL_ON_WARN", "0") == "1"
    if runtime.get("name") == strict_profile:
        if snapshot.overall == "fail":
            if "health-watchdog-fail" not in reasons:
                reasons.append("health-watchdog-fail")
            ready = False
        elif fail_on_warn and snapshot.overall == "warn":
            if "health-watchdog-warn" not in reasons:
                reasons.append("health-watchdog-warn")
            ready = False
    result["ready"] = ready
    result["reasons"] = reasons
    result["watchdog"] = _watchdog_payload(snapshot)
    return result


def _build_health_payload(snapshot: Mapping[str, Any], watchdog: HealthSnapshot) -> Dict[str, Any]:
    result = {
        "ok": bool(snapshot.get("ok")),
        "journal_ok": bool(snapshot.get("journal_ok", True)),
        "resume_ok": bool(snapshot.get("resume_ok", True)),
        "leader": bool(snapshot.get("leader", True)),
        "config_ok": bool(snapshot.get("config_ok", True)),
    }
    result["watchdog"] = _watchdog_payload(watchdog)
    return result


def _replace_router_routes(
    router, *, original_endpoint, new_endpoint
) -> None:  # pragma: no cover - FastAPI internals
    try:
        from fastapi.routing import APIRoute
    except Exception:  # pragma: no cover - defensive
        return
    preserved = []
    for route in list(router.routes):
        endpoint = getattr(route, "endpoint", None)
        if endpoint is original_endpoint and isinstance(route, APIRoute):
            continue
        if isinstance(route, APIRoute) and route.path in {
            "/health",
            "/healthz",
            "/api/health",
            "/api/healthz",
        }:
            continue
        preserved.append(route)
    router.routes = preserved
    for path in ("/healthz", "/health", "/api/healthz", "/api/health"):
        router.add_api_route(
            path,
            new_endpoint,
            methods=["GET"],
            include_in_schema=False,
        )


def _replace_app_routes(new_endpoint) -> bool:  # pragma: no cover - FastAPI internals
    main_module = sys.modules.get("app.main")
    if not main_module or not hasattr(main_module, "app"):
        return False
    app = getattr(main_module, "app")
    try:
        from fastapi.routing import APIRoute
    except Exception:  # pragma: no cover - defensive
        return False
    preserved = []
    for route in list(app.router.routes):
        if not isinstance(route, APIRoute):
            preserved.append(route)
            continue
        if route.path in {"/health", "/healthz", "/api/health", "/api/healthz"}:
            continue
        preserved.append(route)
    app.router.routes = preserved
    for path in ("/healthz", "/health", "/api/healthz", "/api/health"):
        app.add_api_route(path, new_endpoint, methods=["GET"], include_in_schema=False)
    return True


def ensure_watchdog_integration() -> None:
    global _PATCHED, _APP_PATCHED, _ORIGINAL_COMPUTE_READINESS, _ORIGINAL_HEALTH_ENDPOINT

    from app.services import live_readiness as live_readiness_module
    from app.routers import health as health_router
    from app.routers import live as live_router

    if not _PATCHED:
        if _ORIGINAL_COMPUTE_READINESS is None:
            _ORIGINAL_COMPUTE_READINESS = live_readiness_module.compute_readiness

            @wraps(_ORIGINAL_COMPUTE_READINESS)
            def _wrapped_compute_readiness(app) -> Dict[str, Any]:
                payload = _ORIGINAL_COMPUTE_READINESS(app)
                watchdog_snapshot = get_watchdog().snapshot()
                _update_watchdog_metrics(watchdog_snapshot)
                return _append_watchdog_to_readiness(dict(payload), watchdog_snapshot)

            live_readiness_module.compute_readiness = _wrapped_compute_readiness
            try:
                live_router.compute_readiness = _wrapped_compute_readiness
            except Exception:  # pragma: no cover - defensive
                pass

        if _ORIGINAL_HEALTH_ENDPOINT is None:
            _ORIGINAL_HEALTH_ENDPOINT = health_router.health

            @wraps(_ORIGINAL_HEALTH_ENDPOINT)
            def _health_endpoint(request: Request):
                snapshot = health_router.evaluate_health(request.app)
                watchdog_snapshot = get_watchdog().snapshot()
                _update_watchdog_metrics(watchdog_snapshot)
                payload = _build_health_payload(snapshot, watchdog_snapshot)
                status_code = (
                    status.HTTP_200_OK if payload.get("ok") else status.HTTP_503_SERVICE_UNAVAILABLE
                )
                return JSONResponse(status_code=status_code, content=payload)

            _health_endpoint._watchdog_patched = True  # type: ignore[attr-defined]
            health_router.health = _health_endpoint
            health_router.health_alias = _health_endpoint
            _replace_router_routes(
                health_router.router,
                original_endpoint=_ORIGINAL_HEALTH_ENDPOINT,
                new_endpoint=_health_endpoint,
            )

        _PATCHED = True

    if not _APP_PATCHED and _ORIGINAL_HEALTH_ENDPOINT is not None:
        patched = _replace_app_routes(health_router.health)
        if patched:
            _APP_PATCHED = True
