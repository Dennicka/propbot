from __future__ import annotations
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

try:
    from ..services.runtime import get_auto_hedge_state, get_last_opportunity_state
except Exception:  # pragma: no cover - defensive import
    get_auto_hedge_state = None  # type: ignore[assignment]
    get_last_opportunity_state = None  # type: ignore[assignment]

router = APIRouter()

class HealthOut(BaseModel):
    ok: bool


def _is_task_running(task) -> bool:
    if task is None:
        return False
    if hasattr(task, "done") and callable(task.done):
        if task.done():
            return False
    if hasattr(task, "cancelled") and callable(task.cancelled):
        if task.cancelled():
            return False
    return True


def _auto_hedge_healthy(app) -> bool:
    auto_daemon = getattr(app.state, "auto_hedge_daemon", None)
    if _is_task_running(getattr(auto_daemon, "_task", None)):
        return True
    if get_auto_hedge_state is None:  # pragma: no cover - import guard
        return False
    try:
        state = get_auto_hedge_state()
    except Exception:  # pragma: no cover - defensive fallback
        return False
    last_result = getattr(state, "last_execution_result", "") or ""
    if isinstance(last_result, str) and last_result.startswith("error"):
        return False
    return True


def _scanner_healthy(app) -> bool:
    scanner = getattr(app.state, "opportunity_scanner", None)
    if _is_task_running(getattr(scanner, "_task", None)):
        return True
    if get_last_opportunity_state is None:  # pragma: no cover - import guard
        return False
    try:
        get_last_opportunity_state()
    except Exception:  # pragma: no cover - defensive fallback
        return False
    return True


@router.get("/healthz", response_model=HealthOut, include_in_schema=False)
def health(request: Request):
    app = request.app
    checks = [_auto_hedge_healthy(app), _scanner_healthy(app)]

    ok = all(checks)
    if ok:
        return HealthOut(ok=True)
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"ok": False})


@router.get("/health", response_model=HealthOut, include_in_schema=False)
def health_alias(request: Request):  # pragma: no cover - backwards compatibility
    return health(request)
