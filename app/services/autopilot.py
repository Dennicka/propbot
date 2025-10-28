from __future__ import annotations

import logging

from fastapi import FastAPI

from .. import ledger
from ..opsbot.notifier import emit_alert
from .loop import resume_loop
from .runtime import (
    autopilot_apply_resume,
    autopilot_mark_action,
    get_autopilot_state,
    get_state,
)


LOGGER = logging.getLogger(__name__)


def _check_blockers(state) -> str | None:
    autopilot = state.autopilot
    if str(autopilot.target_mode or "").upper() != "RUN":
        return "previous_mode_not_run"
    if bool(autopilot.target_safe_mode):
        return "previous_safe_mode_true"
    safety = state.safety
    if not safety.hold_active:
        return "hold_not_active"
    hold_reason = (safety.hold_reason or "").strip()
    if hold_reason and hold_reason != "restart_safe_mode":
        return f"hold_reason={hold_reason}"
    if not state.control.preflight_passed:
        return "preflight_not_passed"
    guard = state.guards.get("runaway_breaker")
    if guard and str(guard.status or "").upper() == "HOLD":
        return "runaway_guard_active"
    counters = safety.counters
    limits = safety.limits
    if limits.max_orders_per_min and counters.orders_placed_last_min >= limits.max_orders_per_min:
        return "runaway_orders_limit"
    if limits.max_cancels_per_min and counters.cancels_last_min >= limits.max_cancels_per_min:
        return "runaway_cancels_limit"
    if state.risk.breaches:
        return "risk_breach_active"
    auto_state = state.auto_hedge
    if auto_state.enabled:
        last_result = str(auto_state.last_execution_result or "").lower()
        if last_result.startswith("error") or "error" in last_result:
            return "auto_hedge_error"
    derivatives = state.derivatives
    if derivatives and derivatives.venues:
        for venue_id, runtime in derivatives.venues.items():
            try:
                if not runtime.client.ping():
                    return f"exchange_unreachable:{venue_id}"
            except Exception:
                return f"exchange_unreachable:{venue_id}"
    return None


async def evaluate_startup() -> None:
    state = get_state()
    autopilot = get_autopilot_state()
    if not autopilot.enabled:
        LOGGER.info("autopilot disabled; startup resume skipped")
        autopilot_mark_action("disabled", "autopilot_disabled", armed=False)
        return
    blocker = _check_blockers(state)
    if blocker:
        LOGGER.warning("autopilot refused to arm: %s", blocker)
        autopilot_mark_action("refused", blocker, armed=False)
        ledger.record_event(
            level="WARNING",
            code="autopilot_resume_refused",
            payload={"initiator": "autopilot", "reason": blocker},
        )
        try:
            emit_alert("autopilot_refused", f"AUTOPILOT refused to arm (reason={blocker})", {"reason": blocker})
        except Exception:
            LOGGER.debug("failed to emit autopilot refusal alert", exc_info=True)
        return
    resume_reason = state.safety.hold_reason or "startup"
    result = autopilot_apply_resume(safe_mode=autopilot.target_safe_mode)
    autopilot_mark_action("resume", resume_reason, armed=True)
    ledger.record_event(
        level="INFO",
        code="autopilot_resume",
        payload={
            "initiator": "autopilot",
            "reason": resume_reason,
            "hold_cleared": bool(result.get("hold_cleared")),
            "safe_mode": bool(autopilot.target_safe_mode),
        },
    )
    try:
        emit_alert(
            "autopilot_resumed",
            f"AUTOPILOT: resumed trading after restart (reason={resume_reason})",
            {"reason": resume_reason},
        )
    except Exception:
        LOGGER.debug("failed to emit autopilot resume alert", exc_info=True)
    try:
        await resume_loop()
    except Exception:
        LOGGER.exception("autopilot resume loop failed")


def setup_autopilot(app: FastAPI) -> None:
    @app.on_event("startup")
    async def _autopilot_startup() -> None:  # pragma: no cover - integration hook
        await evaluate_startup()


__all__ = ["evaluate_startup", "setup_autopilot"]
