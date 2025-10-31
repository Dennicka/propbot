from __future__ import annotations
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

try:
    from ..services.runtime import get_auto_hedge_state, get_last_opportunity_state
except Exception:  # pragma: no cover - defensive import
    get_auto_hedge_state = None  # type: ignore[assignment]
    get_last_opportunity_state = None  # type: ignore[assignment]

from ..journal import is_enabled as journal_enabled
from ..journal import order_journal

router = APIRouter()

class HealthOut(BaseModel):
    ok: bool
    journal_ok: bool
    resume_ok: bool


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
    task_running = _is_task_running(getattr(auto_daemon, "_task", None))
    if get_auto_hedge_state is None:  # pragma: no cover - import guard
        return task_running
    try:
        state = get_auto_hedge_state()
    except Exception:  # pragma: no cover - defensive fallback
        return False
    enabled = bool(getattr(state, "enabled", False))
    last_result = str(getattr(state, "last_execution_result", "") or "")
    if not enabled:
        return True
    if not task_running:
        return False
    if last_result.lower().startswith("error"):
        return False
    return True


def _scanner_healthy(app) -> bool:
    scanner = getattr(app.state, "opportunity_scanner", None)
    task_running = _is_task_running(getattr(scanner, "_task", None))
    if scanner is None:
        return True
    if not task_running:
        return False
    if get_last_opportunity_state is None:  # pragma: no cover - import guard
        return task_running
    try:
        _, status = get_last_opportunity_state()
    except Exception:  # pragma: no cover - defensive fallback
        return False
    if isinstance(status, str) and status.lower().startswith("error"):
        return False
    return True


@router.get("/healthz", response_model=HealthOut, include_in_schema=False)
def health(request: Request):
    app = request.app
    resume_ok = bool(getattr(app.state, "resume_ok", True))
    journal_ok = order_journal.healthcheck() if journal_enabled() else True
    checks = [_auto_hedge_healthy(app), _scanner_healthy(app), resume_ok, journal_ok]

    ok = all(checks)
    if ok:
        return HealthOut(ok=True, journal_ok=journal_ok, resume_ok=resume_ok)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"ok": False, "journal_ok": journal_ok, "resume_ok": resume_ok},
    )


@router.get("/health", response_model=HealthOut, include_in_schema=False)
def health_alias(request: Request):  # pragma: no cover - backwards compatibility
    return health(request)
