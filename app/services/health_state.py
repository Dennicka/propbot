"""Reusable health-check helpers for API endpoints."""

from __future__ import annotations

from typing import Any, Mapping

from ..journal import is_enabled as journal_enabled
from ..journal import order_journal
from ..runtime.leader_lock import feature_enabled as leader_lock_enabled
from ..runtime.leader_lock import is_leader as leader_lock_is_leader
from .config_state import validate_active_config

try:
    from ..services.runtime import get_auto_hedge_state, get_last_opportunity_state
except Exception:  # pragma: no cover - defensive import
    get_auto_hedge_state = None  # type: ignore[assignment]
    get_last_opportunity_state = None  # type: ignore[assignment]


def _is_task_running(task: Any) -> bool:
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
    auto_daemon = getattr(app.state, "auto_hedge_daemon", None) if app else None
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
    scanner = getattr(app.state, "opportunity_scanner", None) if app else None
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
    if isinstance(status, Mapping):
        status_value = str(status.get("status") or "")
    else:
        status_value = str(status or "")
    if status_value.lower().startswith("error"):
        return False
    return True


def evaluate_health(app) -> dict[str, object]:
    """Return a structured snapshot of system health."""

    resume_ok = bool(getattr(getattr(app, "state", None), "resume_ok", True)) if app else True
    journal_ok = order_journal.healthcheck() if journal_enabled() else True
    auto_ok = _auto_hedge_healthy(app)
    scanner_ok = _scanner_healthy(app)
    leader_ok = leader_lock_is_leader() if leader_lock_enabled() else True
    config_ok, config_errors = validate_active_config()

    ok = all([auto_ok, scanner_ok, resume_ok, journal_ok])
    return {
        "ok": ok,
        "auto_ok": auto_ok,
        "scanner_ok": scanner_ok,
        "resume_ok": resume_ok,
        "journal_ok": journal_ok,
        "leader": leader_ok,
        "config_ok": config_ok,
        "config_errors": config_errors,
    }


__all__ = ["evaluate_health"]
