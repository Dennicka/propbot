from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from html import escape
from typing import Any, Dict, Mapping, Sequence

from fastapi import Request

from ..audit_log import list_recent_operator_actions
from ..opsbot import notifier
from ..runtime_state_store import load_runtime_payload
from ..pnl_report import build_pnl_snapshot
from ..version import APP_VERSION
from ..orchestrator import orchestrator as strategy_orchestrator
from .approvals_store import list_requests as list_pending_requests
from .audit_log import list_recent_events
from . import risk_alerts, risk_guard
from .runtime import (
    get_auto_hedge_state,
    get_last_opportunity_state,
    get_liquidity_status,
    get_reconciliation_status,
    get_state,
)
from .positions_view import build_positions_snapshot
from positions import list_positions
from pnl_history_store import list_recent as list_recent_snapshots
from services import adaptive_risk_advisor
from services.edge_guard import (
    allowed_to_trade as edge_guard_allowed,
    current_context as edge_guard_current_context,
)
from services.execution_stats_store import (
    list_recent as list_recent_execution_stats,
)
from services.daily_reporter import load_latest_report
from ..risk_snapshot import build_risk_snapshot
from ..strategy_budget import get_strategy_budget_manager
from ..strategy_risk import get_strategy_risk_manager


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _trend_summary(history: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not history:
        return {}

    latest = dict(history[0]) if isinstance(history[0], Mapping) else {}
    previous = dict(history[1]) if len(history) > 1 and isinstance(history[1], Mapping) else None

    current_pnl = _coerce_float(latest.get("unrealized_pnl_total"))
    current_exposure = _coerce_float(latest.get("total_exposure_usd_total"))
    previous_pnl = _coerce_float(previous.get("unrealized_pnl_total")) if previous else None
    previous_exposure = _coerce_float(previous.get("total_exposure_usd_total")) if previous else None

    pnl_delta = None if previous is None else current_pnl - (previous_pnl or 0.0)
    exposure_delta = None if previous is None else current_exposure - (previous_exposure or 0.0)

    simulated_payload = latest.get("simulated") if isinstance(latest.get("simulated"), Mapping) else {}

    return {
        "history": [dict(entry) for entry in history if isinstance(entry, Mapping)],
        "latest": latest,
        "previous": previous,
        "current_pnl": current_pnl,
        "current_exposure": current_exposure,
        "previous_pnl": previous_pnl,
        "previous_exposure": previous_exposure,
        "pnl_delta": pnl_delta,
        "exposure_delta": exposure_delta,
        "pnl_improved": True if pnl_delta is None else pnl_delta >= 0.0,
        "exposure_improved": True if exposure_delta is None else exposure_delta <= 0.0,
        "per_venue": dict(latest.get("total_exposure_usd") or {}),
        "simulated_per_venue": dict(simulated_payload.get("per_venue") or {}),
        "simulated_total": _coerce_float(simulated_payload.get("total")),
        "open_positions": _coerce_int(latest.get("open_positions")),
        "partial_positions": _coerce_int(latest.get("partial_positions")),
        "simulated_positions": _coerce_int(simulated_payload.get("positions")),
    }


def _execution_quality_summary(history: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    if not history:
        return {"history": [], "success_rate": None, "per_venue": {}}

    records = [dict(entry) for entry in history if isinstance(entry, Mapping)]
    total = len(records)
    successes = sum(1 for entry in records if bool(entry.get("success")))
    success_rate = successes / total if total else None

    per_venue: Dict[str, Dict[str, float]] = {}
    for entry in records:
        venue = str(entry.get("venue") or "unknown").lower()
        stats = per_venue.setdefault(venue, {"total": 0, "failures": 0})
        stats["total"] += 1
        if not bool(entry.get("success")):
            stats["failures"] += 1
    for venue, stats in per_venue.items():
        total_count = stats.get("total", 0) or 0
        failures = stats.get("failures", 0) or 0
        if total_count:
            stats["failure_rate"] = failures / total_count
        else:
            stats["failure_rate"] = None

    return {
        "history": records[::-1],  # newest first for rendering
        "success_rate": success_rate,
        "per_venue": per_venue,
    }


def _task_running(task: Any) -> bool:
    if task is None:
        return False
    if hasattr(task, "done") and callable(task.done):
        if task.done():
            return False
    if hasattr(task, "cancelled") and callable(task.cancelled):
        if task.cancelled():
            return False
    return True


def _auto_hedge_health(app, auto_state) -> Dict[str, Any]:
    daemon = getattr(app.state, "auto_hedge_daemon", None)
    task = getattr(daemon, "_task", None)
    enabled = bool(getattr(auto_state, "enabled", False))
    last_result = str(getattr(auto_state, "last_execution_result", "") or "")
    if not enabled:
        return {
            "name": "auto_hedge_daemon",
            "ok": True,
            "detail": "disabled",
            "task_running": _task_running(task),
        }
    task_ok = _task_running(task)
    if not task_ok:
        return {
            "name": "auto_hedge_daemon",
            "ok": False,
            "detail": "task not running",
            "task_running": False,
        }
    if last_result.lower().startswith("error"):
        return {
            "name": "auto_hedge_daemon",
            "ok": False,
            "detail": last_result,
            "task_running": True,
        }
    return {
        "name": "auto_hedge_daemon",
        "ok": True,
        "detail": last_result or "healthy",
        "task_running": True,
    }


def _scanner_health(app) -> Dict[str, Any]:
    scanner = getattr(app.state, "opportunity_scanner", None)
    task = getattr(scanner, "_task", None)
    task_ok = _task_running(task)
    last_opportunity, status = get_last_opportunity_state()
    status_text = str(status or "idle")
    if scanner is None:
        return {
            "name": "scanner",
            "ok": True,
            "detail": "not configured",
            "task_running": False,
        }
    if not task_ok:
        return {
            "name": "scanner",
            "ok": False,
            "detail": "task not running",
            "task_running": False,
        }
    if status_text.lower().startswith("error"):
        return {
            "name": "scanner",
            "ok": False,
            "detail": status_text,
            "task_running": True,
        }
    if last_opportunity is None and status_text == "blocked_by_risk":
        detail = "blocked_by_risk"
    else:
        detail = status_text or "healthy"
    return {
        "name": "scanner",
        "ok": True,
        "detail": detail,
        "task_running": True,
    }


def _risk_limits_snapshot() -> Dict[str, float]:
    return {
        "MAX_OPEN_POSITIONS": float(_env_int("MAX_OPEN_POSITIONS", 3)),
        "MAX_NOTIONAL_PER_POSITION_USDT": _env_float(
            "MAX_NOTIONAL_PER_POSITION_USDT", 50_000.0
        ),
        "MAX_TOTAL_NOTIONAL_USDT": _env_float("MAX_TOTAL_NOTIONAL_USDT", 150_000.0),
        "MAX_TOTAL_NOTIONAL_USD": _env_float("MAX_TOTAL_NOTIONAL_USD", 0.0),
        "MAX_LEVERAGE": _env_float("MAX_LEVERAGE", 5.0),
    }


def _safety_snapshot(state) -> Dict[str, Any]:
    safety = state.safety
    payload = safety.as_dict()
    payload["limits"] = safety.limits.as_dict()
    payload["counters"] = safety.counters.as_dict()
    payload["safe_mode"] = bool(getattr(state.control, "safe_mode", False))
    payload["dry_run_mode"] = bool(getattr(state.control, "dry_run_mode", False))
    payload["dry_run"] = bool(getattr(state.control, "dry_run", False))
    return payload


async def build_dashboard_context(request: Request) -> Dict[str, Any]:
    state = get_state()
    persisted = load_runtime_payload()
    auto_state = get_auto_hedge_state()
    positions_snapshot_source = list_positions()
    positions_payload = await build_positions_snapshot(state, positions_snapshot_source)
    pnl_snapshot = build_pnl_snapshot(positions_payload)
    risk_snapshot = await build_risk_snapshot()
    strategy_risk_snapshot = get_strategy_risk_manager().full_snapshot()
    strategy_budget_snapshot = get_strategy_budget_manager().snapshot()
    safety_payload = _safety_snapshot(state)
    persisted_safety = (
        persisted.get("safety") if isinstance(persisted, Mapping) else None
    )
    if isinstance(persisted_safety, Mapping):
        for key, value in persisted_safety.items():
            safety_payload.setdefault(key, value)

    risk_limits_env = _risk_limits_snapshot()
    risk_state = asdict(state.risk.limits)

    approvals = list_pending_requests(status="pending")

    health_checks = [
        _auto_hedge_health(request.app, auto_state),
        _scanner_health(request.app),
    ]

    control_flags = state.control.flags
    active_alerts = risk_alerts.evaluate_alerts()
    recent_audit = notifier.get_recent_alerts(limit=10)
    last_watchdog_alert: dict[str, str] | None = None
    for entry in recent_audit:
        kind = str(entry.get("kind") or "").strip().lower()
        if kind != "watchdog_alert":
            continue
        extra_payload = entry.get("extra") if isinstance(entry.get("extra"), Mapping) else {}
        exchange_value = extra_payload.get("exchange") if isinstance(extra_payload, Mapping) else None
        reason_value = extra_payload.get("reason") if isinstance(extra_payload, Mapping) else None
        timestamp_value = extra_payload.get("timestamp") if isinstance(extra_payload, Mapping) else None
        last_watchdog_alert = {
            "exchange": str(exchange_value or entry.get("exchange") or ""),
            "reason": str(reason_value or entry.get("text") or ""),
            "timestamp": str(timestamp_value or entry.get("ts") or ""),
        }
        break
    try:
        recent_operator_actions = list_recent_operator_actions(limit=5)
    except Exception:
        recent_operator_actions = []
    recent_ops_incidents = list_recent_events(limit=10)

    hold_reason = str(safety_payload.get("hold_reason") or "")
    hold_reason_display = _format_hold_reason(hold_reason)
    risk_throttled = bool(
        safety_payload.get("hold_active")
        and hold_reason.upper().startswith(risk_guard.AUTO_THROTTLE_PREFIX)
    )
    safety_payload["hold_reason_display"] = hold_reason_display

    pnl_history = list_recent_snapshots(limit=5)
    pnl_trend = _trend_summary(pnl_history)
    execution_history = list_recent_execution_stats(limit=15)
    execution_quality = _execution_quality_summary(execution_history)
    try:
        daily_report = load_latest_report()
    except Exception:
        daily_report = None

    guard_allowed, guard_reason = edge_guard_allowed()
    guard_context = edge_guard_current_context()
    liquidity_status = get_liquidity_status()
    reconciliation_status = get_reconciliation_status()
    autopilot_state = state.autopilot.as_dict()

    try:
        strategy_plan = strategy_orchestrator.compute_next_plan()
    except Exception as exc:  # pragma: no cover - defensive guard
        strategy_plan = {"error": str(exc)}

    hold_info = {
        "hold_active": safety_payload.get("hold_active"),
        "hold_reason": hold_reason_display or hold_reason,
        "hold_since": safety_payload.get("hold_since"),
        "last_released_ts": safety_payload.get("last_released_ts"),
        "hold_reason_raw": hold_reason,
    }
    summary_highlights: list[str] = []
    exchange_watchdog_hold_reason = ""
    lowered_reason = hold_reason.lower()
    if lowered_reason.startswith("exchange_watchdog:"):
        detail = hold_reason.split(":", 1)[1].strip() if ":" in hold_reason else ""
        display_detail = detail or hold_reason_display or hold_reason
        exchange_watchdog_hold_reason = display_detail
        if display_detail:
            summary_highlights.append(
                f"Auto-HOLD by exchange watchdog: {display_detail}"
            )
    limits_for_advisor = {
        "MAX_TOTAL_NOTIONAL_USDT": risk_limits_env.get("MAX_TOTAL_NOTIONAL_USDT"),
        "MAX_OPEN_POSITIONS": risk_limits_env.get("MAX_OPEN_POSITIONS"),
    }
    risk_advice = adaptive_risk_advisor.generate_risk_advice(
        pnl_history,
        current_limits=limits_for_advisor,
        hold_info=hold_info,
        dry_run_mode=getattr(state.control, "dry_run_mode", False),
        risk_throttled=risk_throttled,
    )

    strategy_budgets: list[dict[str, object]] = []
    for strategy_name in sorted(strategy_budget_snapshot):
        entry = strategy_budget_snapshot[strategy_name]
        strategy_budgets.append(
            {
                "strategy": strategy_name,
                "max_notional_usdt": entry.get("max_notional_usdt"),
                "current_notional_usdt": entry.get("current_notional_usdt"),
                "max_open_positions": entry.get("max_open_positions"),
                "current_open_positions": entry.get("current_open_positions"),
                "blocked": bool(entry.get("blocked")),
            }
        )

    return {
        "request": request,
        "build_version": APP_VERSION,
        "control": {
            "mode": state.control.mode,
            "safe_mode": state.control.safe_mode,
            "dry_run": state.control.dry_run,
            "dry_run_mode": getattr(state.control, "dry_run_mode", False),
            "two_man_rule": getattr(state.control, "two_man_rule", True),
            "flags": control_flags,
        },
        "safety": safety_payload,
        "auto_hedge": auto_state.as_dict(),
        "risk_limits_env": risk_limits_env,
        "risk_limits_state": risk_state,
        "positions": positions_payload.get("positions", []),
        "exposure": positions_payload.get("exposure", {}),
        "position_totals": positions_payload.get("totals", {}),
        "health_checks": health_checks,
        "autopilot": autopilot_state,
        "pending_approvals": approvals,
        "persisted_snapshot": persisted,
        "active_alerts": active_alerts,
        "recent_audit": recent_audit,
        "last_watchdog_alert": last_watchdog_alert,
        "recent_operator_actions": recent_operator_actions,
        "recent_ops_incidents": recent_ops_incidents,
        "liquidity": liquidity_status,
        "reconciliation": reconciliation_status,
        "risk_throttled": risk_throttled,
        "risk_throttle_reason": hold_reason if risk_throttled else "",
        "edge_guard": {
            "allowed": guard_allowed,
            "reason": guard_reason,
            "context": asdict(guard_context),
        },
        "pnl_history": pnl_history,
        "pnl_trend": pnl_trend,
        "pnl_snapshot": pnl_snapshot,
        "risk_advice": risk_advice,
        "execution_quality": execution_quality,
        "daily_report": daily_report or {},
        "risk_snapshot": risk_snapshot,
        "strategy_plan": strategy_plan,
        "strategy_risk_snapshot": strategy_risk_snapshot,
        "strategy_budgets": strategy_budgets,
        "summary_highlights": summary_highlights,
        "exchange_watchdog_hold_reason": exchange_watchdog_hold_reason,
    }


def _bool_pill(flag: bool, *, true: str, false: str) -> str:
    return true if flag else false


def _fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return ("{:.6f}".format(value)).rstrip("0").rstrip(".") or "0"
    return escape(str(value))


def _format_hold_reason(reason: str) -> str:
    cleaned = str(reason or "").strip()
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if lowered.startswith("exchange_watchdog:"):
        detail = cleaned.split(":", 1)[1].strip() if ":" in cleaned else ""
        if not detail:
            detail = "exchange issue"
        return f"exchange watchdog — {detail}"
    return cleaned


def _status_span(ok: bool) -> str:
    if ok:
        return '<span style="color:#1b7f3b;font-weight:600;">OK</span>'
    return '<span style="color:#b00020;font-weight:700;">DEAD</span>'


def _tag(text: str, *, color: str, weight: str = "700") -> str:
    return f'<span style="color:{color};font-weight:{weight};margin-left:0.5rem;">{escape(text)}</span>'


def _extra_block(extra: object) -> str:
    if not isinstance(extra, Mapping):
        return ""
    payload = {str(key): value for key, value in extra.items()}
    if not payload:
        return ""
    try:
        text = json.dumps(payload, sort_keys=True)
    except (TypeError, ValueError):
        text = str(payload)
    return f"<div style=\"font-size:0.8rem;color:#4b5563;margin-top:0.25rem;\">{escape(text)}</div>"


def _operator_action_details(details: object) -> str:
    if isinstance(details, Mapping):
        payload = {str(key): value for key, value in details.items()}
        if not payload:
            return ""
        try:
            return escape(json.dumps(payload, sort_keys=True))
        except (TypeError, ValueError):
            return escape(str(payload))
    if details is None:
        return ""
    return _fmt(details)


def _ops_status_badge(status: object, action: object) -> str:
    status_text = str(status or "").strip().lower()
    action_text = str(action or "").strip().lower()
    if "auto" in action_text and "hold" in action_text:
        return (
            '<span style="background:#fee2e2;color:#991b1b;padding:0.25rem 0.75rem;'
            'border-radius:999px;font-weight:700;">AUTO-HOLD</span>'
        )
    if status_text == "pending":
        return (
            '<span style="background:#fef3c7;color:#92400e;padding:0.25rem 0.75rem;'
            'border-radius:999px;font-weight:700;">PENDING</span>'
        )
    if status_text in {"approved", "applied"}:
        label = "APPROVED" if status_text == "approved" else "APPLIED"
        return (
            f'<span style="background:#dcfce7;color:#166534;padding:0.25rem 0.75rem;'
            f'border-radius:999px;font-weight:700;">{label}</span>'
        )
    label = status_text.upper() or "UNKNOWN"
    return (
        f'<span style="background:#e5e7eb;color:#111827;padding:0.25rem 0.75rem;'
        f'border-radius:999px;font-weight:700;">{escape(label)}</span>'
    )


def _near_limit_tag(current: object, limit: object) -> str:
    try:
        current_value = float(current)
        limit_value = float(limit)
    except (TypeError, ValueError):
        return ""
    if limit_value <= 0:
        return ""
    try:
        ratio = current_value / limit_value
    except ZeroDivisionError:
        return ""
    if ratio >= 0.8:
        return _tag("NEAR LIMIT", color="#b58900")
    return ""


def _trend_delta_cell(delta: float | None, improved: bool) -> str:
    if delta is None:
        return '<span style="color:#555;">n/a</span>'
    if abs(delta) <= 1e-9:
        arrow = "→"
        color = "#555"
    else:
        arrow = "↑" if delta > 0 else "↓"
        color = "#1b7f3b" if improved else "#b00020"
    value_text = _fmt(delta)
    if delta > 0:
        value_text = f"+{value_text}"
    return f'<span style="color:{color};font-weight:600;">{arrow} {value_text}</span>'


def render_dashboard_html(context: Dict[str, Any]) -> str:
    safety = context.get("safety", {}) or {}
    auto = context.get("auto_hedge", {}) or {}
    daily_report = context.get("daily_report", {}) or {}
    risk_limits_env = context.get("risk_limits_env", {}) or {}
    risk_state = context.get("risk_limits_state", {}) or {}
    exposures = context.get("exposure", {}) or {}
    positions = context.get("positions", []) or []
    totals = context.get("position_totals", {}) or {}
    strategy_budgets = context.get("strategy_budgets", []) or []
    health_checks = context.get("health_checks", []) or []
    approvals = context.get("pending_approvals", []) or []
    flash_messages = context.get("flash_messages", []) or []
    risk_throttled = bool(context.get("risk_throttled"))
    throttle_reason = context.get("risk_throttle_reason") or ""
    active_alerts = context.get("active_alerts", []) or []
    recent_audit = context.get("recent_audit", []) or []
    recent_operator_actions = context.get("recent_operator_actions", []) or []
    trend = context.get("pnl_trend", {}) or {}
    liquidity = context.get("liquidity", {}) or {}
    liquidity_blocked = bool(liquidity.get("liquidity_blocked"))
    liquidity_reason = liquidity.get("reason") or ""
    liquidity_snapshot = liquidity.get("per_venue") or {}
    reconciliation = context.get("reconciliation", {}) or {}
    desync_detected = bool(reconciliation.get("desync_detected"))
    reconciliation_issues = reconciliation.get("issues") or []
    issue_count = reconciliation.get("issue_count")
    if issue_count is None:
        issue_count = len(reconciliation_issues)
    else:
        issue_count = _coerce_int(issue_count, len(reconciliation_issues))
    last_recon_ts = reconciliation.get("last_checked")

    recent_ops_incidents = context.get("recent_ops_incidents", []) or []

    risk_advice = context.get("risk_advice", {}) or {}

    pnl_history = context.get("pnl_history", []) or []
    execution_quality = context.get("execution_quality", {}) or {}
    execution_history = execution_quality.get("history") or []
    success_rate = execution_quality.get("success_rate")
    per_venue_quality = execution_quality.get("per_venue") or {}

    edge_guard_status = context.get("edge_guard", {}) or {}
    edge_guard_allowed = bool(edge_guard_status.get("allowed"))
    edge_guard_reason = edge_guard_status.get("reason") or "ok"
    autopilot = context.get("autopilot", {}) or {}
    autopilot_enabled = bool(autopilot.get("enabled"))
    autopilot_action_raw = str(autopilot.get("last_action") or "none")
    autopilot_action = autopilot_action_raw.lower()
    autopilot_reason = autopilot.get("last_reason") or ""
    autopilot_attempt = autopilot.get("last_attempt_ts") or ""
    autopilot_armed = bool(autopilot.get("armed"))

    pnl_snapshot_raw = context.get("pnl_snapshot", {}) or {}
    pnl_snapshot = dict(pnl_snapshot_raw) if isinstance(pnl_snapshot_raw, Mapping) else {}
    unrealized_pnl_value = pnl_snapshot.get("unrealized_pnl_usdt")
    realised_pnl_value = pnl_snapshot.get("realised_pnl_today_usdt")
    total_exposure_value = pnl_snapshot.get("total_exposure_usdt")
    headroom_payload = pnl_snapshot.get("capital_headroom_per_strategy")
    headroom_map = headroom_payload if isinstance(headroom_payload, Mapping) else {}
    capital_snapshot_payload = pnl_snapshot.get("capital_snapshot")
    if isinstance(capital_snapshot_payload, Mapping):
        per_strategy_limits = (
            capital_snapshot_payload.get("per_strategy_limits")
            if isinstance(capital_snapshot_payload.get("per_strategy_limits"), Mapping)
            else {}
        )
        current_usage = (
            capital_snapshot_payload.get("current_usage")
            if isinstance(capital_snapshot_payload.get("current_usage"), Mapping)
            else {}
        )
    else:
        per_strategy_limits = {}
        current_usage = {}

    strategy_plan = context.get("strategy_plan", {}) or {}
    strategy_entries = strategy_plan.get("strategies") or []
    strategy_plan_ts = strategy_plan.get("ts") or ""
    strategy_plan_error = strategy_plan.get("error")
    strategy_risk = strategy_plan.get("risk_gates") or {}

    risk_snapshot = context.get("risk_snapshot", {}) or {}
    risk_snapshot_total = risk_snapshot.get("total_notional_usd")
    risk_snapshot_partial = risk_snapshot.get("partial_hedges_count")
    risk_snapshot_autopilot = bool(risk_snapshot.get("autopilot_enabled"))
    risk_snapshot_score = risk_snapshot.get("risk_score") or "TBD"
    risk_snapshot_per_venue = risk_snapshot.get("per_venue") or {}

    strategy_risk_snapshot = context.get("strategy_risk_snapshot", {}) or {}
    strategy_risk_strategies = strategy_risk_snapshot.get("strategies") or {}
    strategy_risk_ts_raw = strategy_risk_snapshot.get("timestamp")
    if isinstance(strategy_risk_ts_raw, (int, float)):
        strategy_risk_ts = datetime.fromtimestamp(strategy_risk_ts_raw, tz=timezone.utc).isoformat()
    else:
        strategy_risk_ts = str(strategy_risk_ts_raw or "")

    operator_info = context.get("operator", {}) or {}
    operator_name = operator_info.get("name") or "unknown"
    operator_role_raw = str(operator_info.get("role") or "viewer").strip().lower()
    operator_role = "operator" if operator_role_raw == "operator" else "viewer"
    operator_role_label = operator_role.upper()
    is_operator = operator_role == "operator"
    summary_highlights = [
        str(item)
        for item in context.get("summary_highlights", [])
        if isinstance(item, str) and item.strip()
    ]

    parts: list[str] = []
    parts.append(
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\" />"
        "<title>Operator Dashboard</title>"
        "<style>body{font-family:Arial,sans-serif;margin:2rem;background:#f8f9fb;color:#222;}"
        "h1,h2{color:#14365d;}table{border-collapse:collapse;width:100%;margin-bottom:2rem;background:#fff;}"
        "th,td{border:1px solid #d0d5dd;padding:0.5rem 0.75rem;text-align:left;vertical-align:top;}"
        "th{background:#e9eef5;}"
        ".controls{background:#fff;padding:1.5rem;border:1px solid #d0d5dd;}form{margin-bottom:1.5rem;}"
        "label{display:block;font-weight:600;margin-bottom:0.25rem;}"
        "input[type=text]{width:100%;padding:0.5rem;border:1px solid #c1c7d0;border-radius:4px;margin-bottom:0.5rem;}"
        "button{padding:0.5rem 1rem;border:none;border-radius:4px;background:#14365d;color:#fff;cursor:pointer;}"
        "button:hover{background:#0d2440;}"
        ".note{font-size:0.9rem;color:#555;margin-top:-0.5rem;margin-bottom:0.75rem;}"
        ".flash{background:#fff3cd;border:1px solid #f1c232;color:#533f03;padding:0.75rem 1rem;margin-bottom:1.5rem;border-radius:4px;}"
        "footer{margin-top:3rem;font-size:0.8rem;color:#4b5563;text-align:center;}"
        ".footer-warning{color:#9a3412;font-weight:600;}"
        ".operator-meta{background:#fff;padding:1rem 1.5rem;border:1px solid #d0d5dd;margin-bottom:1.5rem;display:flex;gap:2rem;align-items:center;flex-wrap:wrap;}"
        ".operator-meta .label{color:#4b5563;font-weight:600;margin-right:0.5rem;}"
        ".role-badge{padding:0.25rem 0.75rem;border-radius:999px;font-weight:700;text-transform:uppercase;}"
        ".role-operator{background:#dcfce7;color:#166534;}"
        ".role-viewer{background:#fee2e2;color:#991b1b;}"
        ".read-only-banner{margin-bottom:1.5rem;padding:1rem 1.25rem;border:1px solid #fca5a5;background:#fee2e2;color:#7f1d1d;font-weight:700;font-size:1.1rem;border-radius:4px;}"
        ".strategy-risk{background:#fff;padding:1.5rem;border:1px solid #d0d5dd;margin-bottom:2rem;}"
        ".strategy-risk h2{margin-top:0;}"
        ".strategy-risk .breach-ok{color:#166534;font-weight:700;}"
        ".strategy-risk .breach-alert{color:#b91c1c;font-weight:700;}"
        ".strategy-risk .freeze-alert{color:#b91c1c;font-weight:700;margin-top:0.35rem;}"
        ".strategy-risk .enabled-status{display:block;font-weight:700;color:#166534;margin-bottom:0.25rem;}"
        ".strategy-risk .enabled-status-disabled{color:#b91c1c;}"
        ".strategy-risk .manual-disabled{color:#b91c1c;font-weight:700;margin-top:0.25rem;}"
        ".strategy-risk form.strategy-toggle{margin-top:1rem;padding:1rem;border:1px solid #d0d5dd;background:#f9fafb;border-radius:4px;}"
        ".strategy-risk form.strategy-toggle label{margin-top:0.5rem;}"
        ".strategy-risk form.strategy-toggle .toggle-checkbox{display:flex;align-items:center;gap:0.5rem;font-weight:600;margin:0.5rem 0;}"
        ".strategy-risk .risk-state{display:inline-block;font-weight:700;text-transform:uppercase;}"
        ".strategy-risk .risk-state-active{color:#166534;}"
        ".strategy-risk .risk-state-blocked{color:#b91c1c;}"
        ".strategy-risk .risk-state-frozen{color:#b91c1c;}"
        ".strategy-risk .risk-note{font-size:0.85rem;color:#4b5563;margin-top:0.35rem;}"
        ".strategy-risk .risk-note-alert{color:#b91c1c;font-weight:600;}"
        ".strategy-risk .failure-count{font-weight:700;}"
        ".strategy-risk .failure-count-alert{color:#b91c1c;}"
        ".strategy-risk .failure-count-ok{color:#166534;}"
        ".strategy-budgets{background:#fff;padding:1.5rem;border:1px solid #d0d5dd;margin-bottom:2rem;}"
        ".strategy-budgets h2{margin-top:0;}"
        ".strategy-budgets table{margin-top:1rem;}"
        ".strategy-budgets tr.blocked{background:#fee2e2;}"
        ".strategy-budgets .status-ok{color:#166534;font-weight:700;}"
        ".strategy-budgets .status-blocked{color:#b91c1c;font-weight:700;}"
        ".pnl-risk{background:#fff;padding:1.5rem;border:1px solid #d0d5dd;margin-bottom:2rem;}"
        ".pnl-risk h2{margin-top:0;}"
        ".pnl-risk .metric{margin:0.25rem 0;font-size:0.95rem;}"
        ".pnl-risk table{margin-top:1rem;}"
        ".strategy-orchestrator{background:#fff;padding:1.5rem;border:1px solid #d0d5dd;margin-bottom:2rem;}"
        ".strategy-orchestrator h2{margin-top:0;}"
        ".strategy-orchestrator table{margin-top:1rem;}"
        ".strategy-orchestrator-readonly{margin-bottom:0.75rem;font-weight:700;color:#b91c1c;}"
        ".strategy-orchestrator .decision-run{color:#166534;font-weight:700;}"
        ".strategy-orchestrator .decision-cooldown{color:#92400e;font-weight:700;}"
        ".strategy-orchestrator .decision-skip{color:#1f2937;font-weight:700;}"
        ".strategy-orchestrator .decision-skip-critical{color:#991b1b;font-weight:700;}"
        ".strategy-orchestrator .reason-critical{color:#991b1b;font-weight:700;}"
        ".strategy-orchestrator .reason-cooldown{color:#92400e;font-weight:600;}"
        ".strategy-orchestrator .meta{font-size:0.9rem;color:#4b5563;margin-top:0.5rem;}"
        ".risk-snapshot{background:#fff;padding:1.5rem;border:1px solid #d0d5dd;margin-bottom:2rem;}"
        ".risk-snapshot h2{margin-top:0;}"
        ".risk-snapshot table{margin-top:1rem;}"
        ".risk-snapshot .risk-label{font-weight:600;color:#374151;margin-right:0.5rem;}"
        ".risk-snapshot .risk-pill{font-weight:700;}"
        "button:disabled{background:#9ca3af;cursor:not-allowed;}"
        "input:disabled{background:#e5e7eb;color:#6b7280;cursor:not-allowed;}"
        "</style></head><body>"
    )
    parts.append(
        f"<h1>Operator Dashboard</h1><p>Build Version: <strong>{_fmt(context.get('build_version'))}</strong></p>"
    )

    parts.append(
        "<div class=\"operator-meta\">"
        f"<div><span class=\"label\">Operator:</span> <strong>{_fmt(operator_name)}</strong></div>"
        f"<div><span class=\"label\">Role:</span> <span class=\"role-badge role-{operator_role}\">{_fmt(operator_role_label)}</span></div>"
        "</div>"
    )

    if not is_operator:
        parts.append(
            "<div class=\"read-only-banner\">READ ONLY: you cannot change HOLD/RESUME/KILL.</div>"
        )

    for message in flash_messages:
        parts.append(f"<div class=\"flash\">{_fmt(message)}</div>")
    for highlight in summary_highlights:
        parts.append(
            "<div class=\"flash\" style=\"background:#fee2e2;border-color:#f87171;color:#7f1d1d;\">"
            f"{_fmt(highlight)}"
            "</div>"
        )

    autopilot_details = [
        f"autopilot_status: <strong>{_fmt('enabled' if autopilot_enabled else 'disabled')}</strong>",
        f"last_autopilot_action: <strong>{_fmt(autopilot_action_raw)}</strong>",
        f"last_autopilot_reason: <strong>{_fmt(autopilot_reason or 'n/a')}</strong>",
    ]
    if autopilot_attempt:
        autopilot_details.append(f"last_attempt: {_fmt(autopilot_attempt)}")
    autopilot_html = [
        "<div style=\"background:#fff;padding:1rem;border:1px solid #d0d5dd;margin-bottom:1.5rem;\">",
        "<strong>Autopilot mode</strong>",
        f"<div style=\"margin-top:0.5rem;font-size:0.9rem;color:#1f2937;\">{' · '.join(autopilot_details)}</div>",
    ]
    if autopilot_enabled and autopilot_armed:
        autopilot_html.append(
            "<div style=\"margin-top:0.75rem;padding:0.75rem 1rem;border-radius:4px;"
            "background:#fef3c7;border:1px solid #f59e0b;color:#92400e;font-weight:700;\">"
            "AUTOPILOT ARMED — trading WITHOUT human two-man approval"
            "</div>"
        )
    elif autopilot_enabled and autopilot_action == "refused":
        autopilot_html.append(
            "<div style=\"margin-top:0.75rem;padding:0.75rem 1rem;border-radius:4px;"
            "background:#fee2e2;border:1px solid #fca5a5;color:#b91c1c;font-weight:600;\">"
            f"AUTOPILOT refused to arm — {_fmt(autopilot_reason or 'reason unknown')}"
            "</div>"
        )
    parts.append("".join(autopilot_html) + "</div>")


    strategy_risk_html = ["<div class=\"strategy-risk\"><h2>Strategy Risk / Breach status</h2>"]
    if strategy_risk_ts:
        strategy_risk_html.append(
            f"<p class=\"note\">Snapshot at {_fmt(strategy_risk_ts)}</p>"
        )
    if not strategy_risk_strategies:
        strategy_risk_html.append("<p class=\"note\">No strategy risk data available.</p>")
    else:
        strategy_risk_html.append(
            "<table><thead><tr><th>Strategy</th><th>Risk state</th><th>Daily loss (current / limit)</th>"
            "<th>Consecutive failures</th><th>Notes</th></tr></thead><tbody>"
        )
        for name in sorted(strategy_risk_strategies):
            entry = strategy_risk_strategies.get(name) or {}
            limits = entry.get("limits") or {}
            state = entry.get("state") or {}
            breach = bool(entry.get("breach"))
            daily_limit = limits.get("daily_loss_usdt")
            realized = state.get("realized_pnl_today")
            failure_limit = limits.get("max_consecutive_failures")
            failure_count = state.get("consecutive_failures")
            frozen = bool(state.get("frozen") or entry.get("frozen"))
            freeze_reason = state.get("freeze_reason") or entry.get("reason") or ""
            enabled_flag = state.get("enabled") if isinstance(state, Mapping) else None
            if enabled_flag is None:
                enabled_flag = entry.get("enabled")
            enabled = bool(enabled_flag) if enabled_flag is not None else True
            risk_state = "active"
            if frozen:
                risk_state = "frozen_by_risk"
            elif freeze_reason:
                risk_state = "blocked_by_risk"
            elif breach:
                risk_state = "blocked_by_risk"
            risk_state_class = {
                "active": "risk-state risk-state-active",
                "blocked_by_risk": "risk-state risk-state-blocked",
                "frozen_by_risk": "risk-state risk-state-frozen",
            }.get(risk_state, "risk-state risk-state-blocked")
            risk_state_cell = f'<span class="{risk_state_class}">{risk_state}</span>'
            enabled_label = "yes" if enabled else "no"
            enabled_class = "enabled-status"
            if not enabled:
                enabled_class += " enabled-status-disabled"
            status_label_parts: list[str] = [
                f'<span class="{enabled_class}">enabled: {enabled_label}</span>'
            ]
            if not enabled:
                status_label_parts.append(
                    '<div class="manual-disabled">MANUAL DISABLED (operator override)</div>'
                )
            if breach:
                status_label_parts.append('<span class="breach-alert">BREACH DETECTED</span>')
                reasons = entry.get("breach_reasons") or []
                status_label_parts.extend(
                    f"<div class=\"risk-note\">{_fmt(reason)}</div>" for reason in reasons if reason
                )
            else:
                status_label_parts.append('<span class="breach-ok">OK</span>')
            if freeze_reason:
                if frozen:
                    status_label_parts.append(
                        f"<div class=\"freeze-alert\">FROZEN by risk: {_fmt(freeze_reason)}</div>"
                    )
                else:
                    status_label_parts.append(
                        f"<div class=\"risk-note risk-note-alert\">blocked reason: {_fmt(freeze_reason)}</div>"
                    )
            failure_class = "failure-count"
            failure_display = "n/a"
            if isinstance(failure_count, (int, float)):
                if failure_count:
                    failure_class += " failure-count-alert"
                else:
                    failure_class += " failure-count-ok"
                failure_display = _fmt(failure_count)
            failure_cell = f'<span class="{failure_class}">{failure_display}</span>'
            if failure_limit is not None:
                failure_cell = f"{failure_cell} / {_fmt(failure_limit)}"
            pnl_cell = _fmt(realized)
            if daily_limit is not None:
                pnl_cell = f"{pnl_cell} (limit {_fmt(daily_limit)})"
            status_label = "".join(status_label_parts)
            strategy_risk_html.append(
                "<tr><td>{name}</td><td>{risk}</td><td>{pnl}</td><td>{failures}</td><td>{status}</td></tr>".format(
                    name=_fmt(name),
                    risk=risk_state_cell,
                    pnl=pnl_cell,
                    failures=failure_cell,
                    status=status_label,
                )
            )
        strategy_risk_html.append("</tbody></table>")
        if is_operator:
            strategy_risk_html.append(
                "<div class=\"note\"><strong>Manual override:</strong> use <code>POST /api/ui/unfreeze-strategy</code> to clear risk freezes. To pause or resume trading manually, submit the toggle form below (records audit trail).</div>"
            )
            strategy_risk_html.append(
                "<form method=\"post\" action=\"/api/ui/set-strategy-enabled\" class=\"strategy-toggle-form\">"
                "<label for=\"strategy-toggle-name\">Strategy identifier</label>"
                "<input id=\"strategy-toggle-name\" name=\"strategy\" type=\"text\" placeholder=\"strategy identifier\" required />"
                "<input type=\"hidden\" name=\"enabled\" value=\"false\" />"
                "<label class=\"toggle-checkbox\"><input type=\"checkbox\" name=\"enabled\" value=\"true\" checked /> <span>Enabled</span></label>"
                "<label for=\"strategy-toggle-reason\">Reason</label>"
                "<input id=\"strategy-toggle-reason\" name=\"reason\" type=\"text\" placeholder=\"reason for toggle\" required />"
                "<button type=\"submit\">Update strategy toggle</button>"
                "</form>"
            )
        else:
            strategy_risk_html.append(
                "<div class=\"note\">Strategy enable/disable controls require operator role. Status is still visible above.</div>"
            )
    parts.append("".join(strategy_risk_html) + "</div>")

    budget_parts = ["<div class=\"strategy-budgets\"><h2>Strategy Budgets</h2>"]
    if strategy_budgets:
        budget_parts.append(
            "<table><thead><tr><th>Strategy</th><th>Notional (current / max)</th><th>Open positions</th><th>Status</th></tr></thead><tbody>"
        )
        for entry in strategy_budgets:
            strategy_name = _fmt(entry.get("strategy"))
            current_notional = _fmt(entry.get("current_notional_usdt"))
            max_notional_value = entry.get("max_notional_usdt")
            max_notional = (
                "&infin;" if max_notional_value in (None, 0) else _fmt(max_notional_value)
            )
            notional_tag = _near_limit_tag(
                entry.get("current_notional_usdt"), max_notional_value
            )
            current_positions = _fmt(entry.get("current_open_positions"))
            max_positions_value = entry.get("max_open_positions")
            max_positions = (
                "&infin;" if max_positions_value in (None, 0) else _fmt(max_positions_value)
            )
            blocked = bool(entry.get("blocked"))
            status_html = (
                '<span class="status-blocked">BLOCKED</span>'
                if blocked
                else '<span class="status-ok">OK</span>'
            )
            row_class = " class=\"blocked\"" if blocked else ""
            budget_parts.append(
                "<tr{row_class}><td>{strategy}</td><td>{notional}{tag}</td><td>{positions}</td><td>{status}</td></tr>".format(
                    row_class=row_class,
                    strategy=strategy_name,
                    notional=f"{current_notional} / {max_notional}",
                    tag=notional_tag,
                    positions=f"{current_positions} / {max_positions}",
                    status=status_html,
                )
            )
        budget_parts.append("</tbody></table>")
    else:
        budget_parts.append(
            "<p class=\"note\">Strategy budget data unavailable.</p>"
        )
    parts.append("".join(budget_parts) + "</div>")



    pnl_parts = ["<div class=\"pnl-risk\"><h2>PnL / Risk</h2>"]
    pnl_parts.append(
        f"<p class=\"metric\"><strong>Unrealised PnL:</strong> {_fmt(unrealized_pnl_value)}</p>"
    )
    pnl_parts.append(
        f"<p class=\"metric\"><strong>Realised PnL (today):</strong> {_fmt(realised_pnl_value)}</p>"
    )
    pnl_parts.append(
        f"<p class=\"metric\"><strong>Total exposure (USDT):</strong> {_fmt(total_exposure_value)}</p>"
    )
    if headroom_map:
        pnl_parts.append(
            "<table><thead><tr><th>Strategy</th><th>Headroom (USDT)</th><th>Limit</th><th>Open notional</th></tr></thead><tbody>"
        )
        for strategy in sorted(headroom_map):
            entry = headroom_map.get(strategy)
            entry_mapping = entry if isinstance(entry, Mapping) else {}
            limit_entry = per_strategy_limits.get(strategy)
            limit_mapping = limit_entry if isinstance(limit_entry, Mapping) else {}
            usage_entry = current_usage.get(strategy)
            usage_mapping = usage_entry if isinstance(usage_entry, Mapping) else {}
            pnl_parts.append(
                "<tr><td>{strategy}</td><td>{headroom}</td><td>{limit}</td><td>{usage}</td></tr>".format(
                    strategy=_fmt(strategy),
                    headroom=_fmt(entry_mapping.get("headroom_notional")),
                    limit=_fmt(limit_mapping.get("max_notional")),
                    usage=_fmt(usage_mapping.get("open_notional")),
                )
            )
        pnl_parts.append("</tbody></table>")
    else:
        pnl_parts.append(
            "<p class=\"note\">Capital headroom data unavailable.</p>"
        )
    parts.append("".join(pnl_parts) + "</div>")

    parts.append("<div class=\"strategy-orchestrator\"><h2>Strategy Orchestrator</h2>")
    if not is_operator:
        parts.append("<div class=\"strategy-orchestrator-readonly\">READ ONLY</div>")
    parts.append(
        "<p class=\"meta\">Orchestrator alerts for skip/risk_limit/hold_active and cooldown/fail "
        "are forwarded to ops Telegram/audit.</p>"
    )
    if strategy_plan_error:
        parts.append(
            "<p class=\"note\" style=\"color:#b91c1c;font-weight:600;\">"
            f"Unable to compute plan: {_fmt(strategy_plan_error)}"
            "</p>"
        )
    else:
        if strategy_plan_ts:
            parts.append(
                f"<div class=\"meta\">Plan computed at <strong>{_fmt(strategy_plan_ts)}</strong></div>"
            )
        if strategy_risk:
            risk_ok = bool(strategy_risk.get("risk_caps_ok", True))
            if risk_ok:
                risk_summary_html = (
                    "<div class=\"meta\"><strong style=\"color:#166534;\">Risk gates: clear</strong></div>"
                )
            else:
                reason_text = _fmt(strategy_risk.get("reason_if_blocked") or "blocked")
                risk_summary_html = (
                    "<div class=\"meta\"><strong style=\"color:#b91c1c;\">Risk gates blocking</strong>"
                    f" — {reason_text}</div>"
                )
            parts.append(risk_summary_html)
        parts.append(
            "<table><thead><tr><th>Strategy</th><th>Decision</th><th>Reason</th><th>Last Result</th>"
            "<th>Last Error</th><th>Last Run</th></tr></thead><tbody>"
        )
        if not strategy_entries:
            parts.append("<tr><td colspan=\"6\">No strategies registered.</td></tr>")
        else:
            for entry in strategy_entries:
                if not isinstance(entry, Mapping):
                    continue
                name = _fmt(entry.get("name"))
                decision_raw = str(entry.get("decision") or "").strip().lower()
                reason_raw = str(entry.get("reason") or "")
                decision_class = "decision-skip"
                if decision_raw == "run":
                    decision_class = "decision-run"
                elif decision_raw == "cooldown":
                    decision_class = "decision-cooldown"
                elif decision_raw == "skip" and reason_raw in {"hold_active", "risk_limit"}:
                    decision_class = "decision-skip-critical"
                reason_class = ""
                if decision_raw == "cooldown":
                    reason_class = "reason-cooldown"
                if decision_raw == "skip" and reason_raw in {"hold_active", "risk_limit"}:
                    reason_class = "reason-critical"
                decision_html = f"<span class=\"{decision_class}\">{_fmt(decision_raw or 'n/a')}</span>"
                reason_html = (
                    f"<span class=\"{reason_class}\">{_fmt(reason_raw)}</span>" if reason_class else _fmt(reason_raw)
                )
                parts.append(
                    "<tr><td>{name}</td><td>{decision}</td><td>{reason}</td><td>{last_result}</td><td>{last_error}</td>"
                    "<td>{last_run}</td></tr>".format(
                        name=name,
                        decision=decision_html,
                        reason=reason_html,
                        last_result=_fmt(entry.get("last_result")),
                        last_error=_fmt(entry.get("last_error")),
                        last_run=_fmt(entry.get("last_run_ts")),
                    )
                )
        parts.append("</tbody></table>")
    parts.append("</div>")

    if risk_throttled:
        reason_clause = (
            f" Trigger: {_fmt(throttle_reason)}." if throttle_reason else ""
        )
        parts.append(
            "<div class=\"flash\" style=\"background:#fee2e2;border:1px solid #b91c1c;color:#7f1d1d;\">"
            "<strong>RISK_THROTTLED</strong> — automatic risk guard hold active. "
            "Manual two-step RESUME approval required before trading can restart."
            f"{reason_clause}"
            "</div>"
        )

    parts.append("<h2>Reconciliation status</h2>")
    if desync_detected:
        parts.append(
            "<div style=\"background:#fee2e2;border:1px solid #b91c1c;color:#7f1d1d;"
            "padding:1rem;border-radius:6px;font-weight:700;font-size:1.1rem;\">"
            "STATE DESYNC — manual intervention required"
            "</div>"
        )
    else:
        parts.append(
            "<p><strong style=\"color:#166534;\">In sync with exchange state.</strong></p>"
        )
    parts.append(
        "<p class=\"note\">Outstanding mismatches: {count}. Resolve manually before resume.</p>".format(
            count=_fmt(issue_count)
        )
    )
    if last_recon_ts:
        parts.append(
            f"<p class=\"note\">Last checked: {_fmt(last_recon_ts)}.</p>"
        )
    if desync_detected and reconciliation_issues:
        visible_issues = reconciliation_issues[:5]
        parts.append("<ul style=\"background:#fff;border:1px solid #fca5a5;padding:0.75rem 1rem;\">")
        for issue in visible_issues:
            summary = "{kind}: {venue} {symbol} {side} — {detail}".format(
                kind=_fmt(issue.get("kind")),
                venue=_fmt(issue.get("venue")),
                symbol=_fmt(issue.get("symbol")),
                side=_fmt(issue.get("side")),
                detail=_fmt(issue.get("description")),
            )
            parts.append(f"<li style=\"margin-bottom:0.5rem;\">{summary}</li>")
        remaining = max(0, issue_count - len(visible_issues))
        if remaining > 0:
            parts.append(
                f"<li style=\"color:#b91c1c;\">+{remaining} more issues not shown</li>"
            )
        parts.append("</ul>")

    parts.append("<h2>Balances / Liquidity</h2>")
    if liquidity_blocked:
        reason_text = liquidity_reason if liquidity_reason and liquidity_reason != "ok" else "insufficient free balance"
        parts.append(
            "<p><strong style=\"color:#b91c1c;\">TRADING HALTED FOR SAFETY — trading halted for safety.</strong></p>"
        )
        parts.append(f"<p class=\"note\">Reason: {_fmt(reason_text)}</p>")
    elif liquidity_reason and liquidity_reason not in {"", "ok"}:
        parts.append(f"<p class=\"note\">Status: {_fmt(liquidity_reason)}</p>")
    if liquidity_snapshot:
        parts.append(
            "<table><thead><tr><th>Venue</th><th>Free USDT</th><th>Used USDT</th><th>Risk OK</th><th>Reason</th></tr></thead><tbody>"
        )
        for venue, payload in sorted(liquidity_snapshot.items()):
            if isinstance(payload, Mapping):
                free_value = payload.get("free_usdt")
                used_value = payload.get("used_usdt")
                risk_flag = bool(payload.get("risk_ok"))
                reason_value = payload.get("reason")
            else:
                free_value = None
                used_value = None
                risk_flag = False
                reason_value = payload
            risk_cell = (
                '<span style="color:#1b7f3b;font-weight:600;">OK</span>'
                if risk_flag
                else '<span style="color:#b91c1c;font-weight:700;">BLOCKED</span>'
            )
            parts.append(
                "<tr><td>{venue}</td><td>{free}</td><td>{used}</td><td>{risk}</td><td>{reason}</td></tr>".format(
                    venue=_fmt(venue),
                    free=_fmt(free_value),
                    used=_fmt(used_value),
                    risk=risk_cell,
                    reason=_fmt(reason_value),
                )
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<p>No balance snapshot available.</p>")

    parts.append("<h2>Active Alerts / Recent Audit</h2>")
    parts.append("<table><thead><tr><th>Alert</th><th>Detail</th><th>Active Since</th></tr></thead><tbody>")
    if not active_alerts:
        parts.append("<tr><td colspan=\"3\">No active risk alerts</td></tr>")
    else:
        for alert in active_alerts:
            text_html = _fmt(alert.get("text"))
            extra_html = _extra_block(alert.get("extra"))
            parts.append(
                "<tr><td>{kind}</td><td>{text}{extra}</td><td>{since}</td></tr>".format(
                    kind=_fmt(alert.get("kind")),
                    text=text_html,
                    extra=extra_html,
                    since=_fmt(alert.get("active_since")),
                )
            )
    parts.append("</tbody></table>")

    parts.append("<table><thead><tr><th>Timestamp</th><th>Event</th><th>Detail</th></tr></thead><tbody>")
    if not recent_audit:
        parts.append("<tr><td colspan=\"3\">No recent audit entries</td></tr>")
    else:
        for entry in recent_audit:
            text_html = _fmt(entry.get("text"))
            extra_html = _extra_block(entry.get("extra"))
            parts.append(
                "<tr><td>{ts}</td><td>{kind}</td><td>{text}{extra}</td></tr>".format(
                    ts=_fmt(entry.get("ts")),
                    kind=_fmt(entry.get("kind")),
                    text=text_html,
                    extra=extra_html,
                )
            )
    parts.append("</tbody></table>")

    parts.append("<h2>Recent Ops / Incidents</h2>")
    parts.append(
        "<table><thead><tr><th>Timestamp</th><th>Actor</th><th>Action</th><th>Status</th><th>Reason</th></tr></thead><tbody>"
    )
    if not recent_ops_incidents:
        parts.append("<tr><td colspan=\"5\">No operational events logged</td></tr>")
    else:
        for entry in recent_ops_incidents:
            status_badge = _ops_status_badge(entry.get("status"), entry.get("action"))
            parts.append(
                "<tr><td>{ts}</td><td>{actor}</td><td>{action}</td><td>{status}</td><td>{reason}</td></tr>".format(
                    ts=_fmt(entry.get("timestamp")),
                    actor=_fmt(entry.get("actor")),
                    action=_fmt(entry.get("action")),
                    status=status_badge,
                    reason=_fmt(entry.get("reason")),
                )
            )
    parts.append("</tbody></table>")

    parts.append("<h2>Risk Advisor Suggestion</h2>")
    if not risk_advice:
        parts.append("<p>No adaptive risk suggestion available yet.</p>")
    else:
        parts.append(
            "<p><strong>Manual two-step approval required:</strong> Suggestions are advisory only."
            " Apply limit changes exclusively via the existing request/approve flow.</p>"
        )
        window_value = risk_advice.get("analysis_window")
        if window_value:
            parts.append(
                f"<p class=\"note\">Analysis window: {_fmt(window_value)} snapshots.</p>"
            )
        parts.append(
            "<table><thead><tr><th>Limit</th><th>Current</th><th>Suggested</th></tr></thead><tbody>"
            f"<tr><td>MAX_TOTAL_NOTIONAL_USDT</td><td>{_fmt(risk_advice.get('current_max_notional'))}</td>"
            f"<td>{_fmt(risk_advice.get('suggested_max_notional'))}</td></tr>"
            f"<tr><td>MAX_OPEN_POSITIONS</td><td>{_fmt(risk_advice.get('current_max_positions'))}</td>"
            f"<td>{_fmt(risk_advice.get('suggested_max_positions'))}</td></tr>"
            "</tbody></table>"
        )
        parts.append(
            "<p><strong>Recommendation:</strong> {}</p>".format(
                _fmt(risk_advice.get("recommendation"))
            )
        )
        reason_text = _fmt(risk_advice.get("reason"))
        if reason_text:
            parts.append(f"<p class=\"note\">Reason: {reason_text}</p>")
        if risk_advice.get("recommend_dry_run_mode"):
            parts.append(
                "<p class=\"note\">Advisor suggests keeping DRY_RUN_MODE engaged while conditions are investigated.</p>"
            )

    parts.append("<h2>Risk &amp; PnL trend</h2>")
    latest_trend = trend.get("latest") if isinstance(trend, Mapping) else None
    if not latest_trend:
        parts.append("<p>No snapshots recorded yet.</p>")
    else:
        timestamp = _fmt(latest_trend.get("timestamp"))
        parts.append(f"<p class=\"note\">Latest snapshot at {timestamp or 'n/a'}.</p>")
        parts.append(
            "<table><thead><tr><th>Metric</th><th>Current</th><th>Δ vs previous</th></tr></thead><tbody>"
        )
        parts.append(
            "<tr><td>Unrealised PnL</td><td>{current}</td><td>{delta}</td></tr>".format(
                current=_fmt(trend.get("current_pnl")),
                delta=_trend_delta_cell(trend.get("pnl_delta"), bool(trend.get("pnl_improved"))),
            )
        )
        parts.append(
            "<tr><td>Total Exposure (USD)</td><td>{current}</td><td>{delta}</td></tr>".format(
                current=_fmt(trend.get("current_exposure")),
                delta=_trend_delta_cell(
                    trend.get("exposure_delta"), bool(trend.get("exposure_improved"))
                ),
            )
        )
        parts.append("</tbody></table>")
        parts.append(
            "<p class=\"note\">Open positions: {open_count} &nbsp; Partial: {partial_count} &nbsp; "
            "Simulated: {sim_positions}</p>".format(
                open_count=_fmt(trend.get("open_positions")),
                partial_count=_fmt(trend.get("partial_positions")),
                sim_positions=_fmt(trend.get("simulated_positions")),
            )
        )
        per_venue = trend.get("per_venue") or {}
        if per_venue:
            per_venue_text = ", ".join(
                f"{escape(str(venue))}: {_fmt(value)}" for venue, value in sorted(per_venue.items())
            )
            parts.append(f"<p class=\"note\">Per-venue exposure: {per_venue_text}</p>")
        simulated_total = trend.get("simulated_total")
        simulated_per_venue = trend.get("simulated_per_venue") or {}
        if simulated_total or simulated_per_venue:
            sim_detail = ""
            if simulated_per_venue:
                sim_detail = ", ".join(
                    f"{escape(str(venue))}: {_fmt(value)}"
                    for venue, value in sorted(simulated_per_venue.items())
                )
                sim_detail = f" (per venue: {sim_detail})"
            parts.append(
                "<p class=\"note\">Simulated exposure total {total}{detail}</p>".format(
                    total=_fmt(simulated_total),
                    detail=sim_detail,
                )
            )

    hold_active = bool(safety.get("hold_active"))
    hold_reason_raw = safety.get("hold_reason")
    hold_reason_display = safety.get("hold_reason_display") or hold_reason_raw
    hold_since = safety.get("hold_since")
    last_watchdog_alert = context.get("last_watchdog_alert") or {}
    parts.append("<h2>Runtime &amp; Safety</h2><table><tbody>")
    mode_value = _fmt(context.get("control", {}).get("mode"))
    if risk_throttled:
        mode_value = f"RISK_THROTTLED ({mode_value})"
    parts.append(f"<tr><th>Mode</th><td>{mode_value}</td></tr>")
    if hold_active:
        detail = "YES"
        reason_text = hold_reason_display or hold_reason_raw
        if reason_text:
            detail += f" - Reason: {_fmt(reason_text)}"
        if hold_since:
            detail += f" (since {_fmt(hold_since)})"
        hold_cell = f'<span style="color:#b00020;font-weight:700;">{detail}</span>'
    else:
        hold_cell = '<span style=\"color:#1b7f3b;font-weight:600;\">NO</span>'
    parts.append(f"<tr><th>HOLD Active</th><td>{hold_cell}</td></tr>")

    if isinstance(last_watchdog_alert, Mapping) and last_watchdog_alert:
        watchdog_cell = (
            f"{_fmt(last_watchdog_alert.get('exchange'))} / "
            f"{_fmt(last_watchdog_alert.get('reason'))} / "
            f"{_fmt(last_watchdog_alert.get('timestamp'))}"
        )
    else:
        watchdog_cell = "n/a"
    parts.append(f"<tr><th>Last watchdog alert</th><td>{watchdog_cell}</td></tr>")

    if edge_guard_allowed:
        guard_cell = '<span style="color:#1b7f3b;font-weight:600;">YES</span>'
        guard_suffix = ""
        if edge_guard_reason not in {"", "ok"}:
            guard_suffix = f" — {_fmt(edge_guard_reason)}"
    else:
        guard_cell = '<span style="color:#b00020;font-weight:700;">NO</span>'
        guard_suffix = f" — {_fmt(edge_guard_reason)}"
    parts.append(
        f"<tr><th>Edge guard status</th><td>{guard_cell}{guard_suffix}</td></tr>"
    )
    parts.append(
        "<tr><th>Safe Mode</th><td>{}</td></tr>".format(
            _bool_pill(bool(context.get("control", {}).get("safe_mode")), true="ON", false="OFF")
        )
    )
    dry_run_flags = []
    if safety.get("dry_run") is not None:
        dry_run_flags.append(f"dry_run={'on' if safety.get('dry_run') else 'off'}")
    if safety.get("dry_run_mode") is not None:
        dry_run_flags.append(
            f"dry_run_mode={'on' if safety.get('dry_run_mode') else 'off'}"
        )
    parts.append(
        "<tr><th>Dry-Run Flags</th><td>{}</td></tr>".format(
            " &nbsp;".join(f"<span>{escape(flag)}</span>" for flag in dry_run_flags) or ""
        )
    )
    counters = safety.get("counters", {})
    limits = safety.get("limits", {})
    orders_current = counters.get("orders_placed_last_min")
    orders_limit = limits.get("max_orders_per_min")
    cancels_current = counters.get("cancels_last_min")
    cancels_limit = limits.get("max_cancels_per_min")
    orders_line = (
        f"Orders: {_fmt(orders_current)} / Limit {_fmt(orders_limit)}"
        f"{_near_limit_tag(orders_current, orders_limit)}"
    )
    cancels_line = (
        f"Cancels: {_fmt(cancels_current)} / Limit {_fmt(cancels_limit)}"
        f"{_near_limit_tag(cancels_current, cancels_limit)}"
    )
    parts.append(
        f"<tr><th>Runaway Counters (last min)</th><td>{orders_line}<br />{cancels_line}</td></tr>"
    )
    resume_request = safety.get("resume_request")
    if isinstance(resume_request, Mapping):
        rr_line = (
            f"Requested by {_fmt(resume_request.get('requested_by') or 'unknown')} at "
            f"{_fmt(resume_request.get('requested_at'))} — reason: {_fmt(resume_request.get('reason'))}"
        )
        parts.append(f"<tr><th>Pending Resume Request</th><td>{rr_line}</td></tr>")
    parts.append("</tbody></table>")

    parts.append("<h3 style=\"margin-top:1rem;\">Recent operator actions</h3>")
    parts.append(
        "<table><thead><tr><th>Timestamp</th><th>Operator</th><th>Action</th><th>Details</th></tr></thead><tbody>"
    )
    if not recent_operator_actions:
        parts.append("<tr><td colspan=\"4\">No operator actions recorded</td></tr>")
    else:
        for action_entry in recent_operator_actions:
            parts.append(
                "<tr><td>{ts}</td><td>{name}</td><td>{action}</td><td>{details}</td></tr>".format(
                    ts=_fmt(action_entry.get("timestamp")),
                    name=_fmt(action_entry.get("operator_name")),
                    action=_fmt(action_entry.get("action")),
                    details=_operator_action_details(action_entry.get("details")),
                )
            )
    parts.append("</tbody></table>")

    risk_autopilot_label = "ENABLED" if risk_snapshot_autopilot else "DISABLED"
    risk_autopilot_color = "#1b7f3b" if risk_snapshot_autopilot else "#b00020"
    risk_autopilot_html = (
        f'<span class="risk-pill" style="color:{risk_autopilot_color};">{risk_autopilot_label}</span>'
    )
    parts.append("<div class=\"risk-snapshot\">")
    parts.append("<h2>Risk snapshot</h2>")
    parts.append(
        f"<p><span class=\"risk-label\">total_notional_usd:</span> <strong>{_fmt(risk_snapshot_total)}</strong></p>"
    )
    parts.append(
        f"<p><span class=\"risk-label\">partial_hedges_count:</span> <strong>{_fmt(risk_snapshot_partial)}</strong></p>"
    )
    parts.append(
        f"<p><span class=\"risk-label\">autopilot_enabled:</span> {risk_autopilot_html}</p>"
    )
    parts.append(
        f"<p><span class=\"risk-label\">risk_score:</span> <strong>{_fmt(risk_snapshot_score)}</strong></p>"
    )
    if risk_snapshot_per_venue:
        parts.append(
            "<table><thead><tr><th>Venue</th><th>net_exposure_usd</th><th>unrealised_pnl_usd</th><th>open_positions_count</th></tr></thead><tbody>"
        )
        for venue, stats in sorted(risk_snapshot_per_venue.items()):
            stats = stats or {}
            parts.append(
                "<tr><td>{venue}</td><td>{exposure}</td><td>{pnl}</td><td>{positions}</td></tr>".format(
                    venue=_fmt(venue),
                    exposure=_fmt(stats.get("net_exposure_usd")),
                    pnl=_fmt(stats.get("unrealised_pnl_usd")),
                    positions=_fmt(stats.get("open_positions_count")),
                )
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<p class=\"note\">No active venues recorded.</p>")
    parts.append("</div>")

    parts.append("<h2>Auto-Hedge</h2><table><tbody>")
    parts.append(f"<tr><th>Enabled</th><td>{'YES' if auto.get('enabled') else 'NO'}</td></tr>")
    parts.append(
        f"<tr><th>Last Execution Result</th><td>{_fmt(auto.get('last_execution_result') or 'n/a')}</td></tr>"
    )
    parts.append(
        f"<tr><th>Last Success</th><td>{_fmt(auto.get('last_success_ts') or 'never')}</td></tr>"
    )
    parts.append(
        f"<tr><th>Consecutive Failures</th><td>{_fmt(auto.get('consecutive_failures'))}</td></tr>"
    )
    parts.append("</tbody></table>")

    parts.append("<h2>Execution Quality</h2>")
    if not execution_history:
        parts.append("<p>No hedge execution attempts recorded yet.</p>")
    else:
        sample_size = len(execution_history)
        if success_rate is None:
            rate_text = "n/a"
        else:
            rate_text = f"{success_rate * 100:.1f}%"
        parts.append(
            f"<p>Success rate (last {sample_size} legs): <strong>{rate_text}</strong></p>"
        )
        parts.append(
            "<table><thead><tr><th>Timestamp</th><th>Venue</th><th>Side</th>"
            "<th>Planned Px</th><th>Fill Px</th><th>Slippage (bps)</th><th>Status</th>"
            "</tr></thead><tbody>"
        )
        for entry in execution_history:
            success = bool(entry.get("success"))
            row_style = ' style="background:#fee2e2;"' if not success else ""
            slippage = entry.get("slippage_bps")
            if isinstance(slippage, (int, float)):
                slippage_text = f"{slippage:+.2f}"
            else:
                slippage_text = "n/a"
            parts.append(
                "<tr{row_style}><td>{ts}</td><td>{venue}</td><td>{side}</td>"
                "<td>{planned}</td><td>{filled}</td><td>{slippage}</td><td>{status}</td></tr>".format(
                    row_style=row_style,
                    ts=_fmt(entry.get("timestamp")),
                    venue=_fmt(entry.get("venue")),
                    side=_fmt(entry.get("side")),
                    planned=_fmt(entry.get("planned_px")),
                    filled=_fmt(entry.get("real_fill_px")),
                    slippage=_fmt(slippage_text),
                    status=_status_span(success),
                )
            )
        parts.append("</tbody></table>")
        parts.append("<h3>Venue Breakdown</h3>")
        parts.append(
            "<table><thead><tr><th>Venue</th><th>Total</th><th>Failures</th><th>Failure Rate</th></tr></thead><tbody>"
        )
        if per_venue_quality:
            for venue, stats in sorted(per_venue_quality.items()):
                failure_rate = stats.get("failure_rate")
                highlight = bool(failure_rate is not None and failure_rate >= 0.3)
                row_style = ' style="background:#fee2e2;"' if highlight else ""
                if failure_rate is None:
                    failure_text = "n/a"
                else:
                    failure_text = f"{failure_rate * 100:.1f}%"
                parts.append(
                    "<tr{row_style}><td>{venue}</td><td>{total}</td><td>{failures}</td><td>{rate}</td></tr>".format(
                        row_style=row_style,
                        venue=_fmt(venue),
                        total=_fmt(stats.get("total")),
                        failures=_fmt(stats.get("failures")),
                        rate=_fmt(failure_text),
                    )
                )
        else:
            parts.append("<tr><td colspan=\"4\">No venues recorded.</td></tr>")
        parts.append("</tbody></table>")

    parts.append("<h2>Risk Limits</h2><table><thead><tr><th>Limit</th><th>Configured Value</th></tr></thead><tbody>")
    for name, value in sorted(risk_limits_env.items()):
        parts.append(f"<tr><td>{_fmt(name)}</td><td>{_fmt(value)}</td></tr>")
    parts.append("</tbody></table>")
    parts.append(f"<p class=\"note\">Runtime risk limits snapshot: {_fmt(risk_state)}</p>")

    parts.append("<h2>Daily PnL / Ops summary</h2>")
    if not daily_report:
        parts.append("<p>No daily report captured in the last 24 hours.</p>")
    else:
        realized_text = _fmt(daily_report.get("pnl_realized_total")) or "0"
        unrealised_text = _fmt(daily_report.get("pnl_unrealized_avg")) or "0"
        exposure_text = _fmt(daily_report.get("exposure_avg")) or "0"
        slippage_avg = daily_report.get("slippage_avg_bps")
        slippage_text = _fmt(slippage_avg) if slippage_avg is not None else "n/a"
        hold_breakdown = (
            daily_report.get("hold_breakdown")
            if isinstance(daily_report.get("hold_breakdown"), Mapping)
            else {}
        )
        hold_total = int(float(daily_report.get("hold_events") or 0))
        auto_holds = int(float(hold_breakdown.get("safety_hold") or 0))
        throttles = int(float(hold_breakdown.get("risk_throttle") or 0))
        parts.append(
            "<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>"
            f"<tr><td>PnL realised (24h)</td><td>{realized_text}</td></tr>"
            f"<tr><td>Unrealised PnL avg</td><td>{unrealised_text}</td></tr>"
            f"<tr><td>Average exposure (USD)</td><td>{exposure_text}</td></tr>"
            f"<tr><td>Average slippage (bps)</td><td>{slippage_text}</td></tr>"
            f"<tr><td>HOLD / throttle events</td><td>{hold_total} (auto {auto_holds}, throttle {throttles})</td></tr>"
            "</tbody></table>"
        )
        window = _fmt(daily_report.get("window_hours")) or "24"
        timestamp = _fmt(daily_report.get("timestamp")) or "n/a"
        pnl_samples = int(float(daily_report.get("pnl_unrealized_samples") or 0))
        exposure_samples = int(float(daily_report.get("exposure_samples") or 0))
        slippage_samples = int(float(daily_report.get("slippage_samples") or 0))
        parts.append(
            "<p class=\"note\">Window: {window}h; last snapshot {ts}. PnL samples: {pnl}, "
            "exposure samples: {exp}; slippage samples: {slip}.</p>".format(
                window=_fmt(window),
                ts=_fmt(timestamp),
                pnl=_fmt(pnl_samples),
                exp=_fmt(exposure_samples),
                slip=_fmt(slippage_samples),
            )
        )

    parts.append("<h2>Exposure (Open / Partial)</h2>")
    if exposures:
        parts.append("<table><thead><tr><th>Venue</th><th>Long Notional</th><th>Short Notional</th><th>Net USDT</th></tr></thead><tbody>")
        for venue, payload in sorted(exposures.items()):
            risk_badge = ""
            try:
                net_value = abs(float(payload.get("net_usdt") or 0.0))
            except (TypeError, ValueError):
                net_value = 0.0
            if net_value > 0.0:
                risk_badge = _tag("OUTSTANDING RISK", color="#b00020")
            parts.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    _fmt(venue),
                    _fmt(payload.get("long_notional")),
                    _fmt(payload.get("short_notional")),
                    f"{_fmt(payload.get('net_usdt'))}{risk_badge}",
                )
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<p>No live exposure recorded.</p>")
    parts.append(
        f"<p class=\"note\">Unrealised hedge PnL: {_fmt(totals.get('unrealized_pnl_usdt'))}</p>"
    )

    parts.append("<h2>Open Hedge Positions</h2>")
    display_positions: list[Mapping[str, Any]] = []
    for position in positions:
        status_value = str(position.get("status") or "").lower()
        if status_value in {"open", "partial"} or bool(position.get("simulated")):
            display_positions.append(position)
    positions = display_positions
    if positions:
        parts.append(
            "<table><thead><tr><th>Symbol</th><th>Status</th><th>Notional (USDT)</th><th>Legs</th><th>Unrealised PnL</th></tr></thead><tbody>"
        )
        for position in positions:
            legs = position.get("legs") or []
            leg_lines = []
            for leg in legs:
                venue = _fmt(leg.get("venue"))
                side = _fmt(leg.get("side"))
                entry_price = _fmt(leg.get("entry_price"))
                mark_price = _fmt(leg.get("mark_price"))
                status_raw = str(leg.get("status") or "")
                status = _fmt(status_raw)
                if status_raw.lower() == "partial":
                    status += _tag("OUTSTANDING RISK", color="#b00020")
                elif status_raw.lower() == "simulated":
                    status += _tag("SIMULATED", color="#555", weight="600")
                leg_lines.append(
                    f"<div>{venue} — {side} @ entry {entry_price} (mark {mark_price}) [{status}]</div>"
                )
            legs_html = "".join(leg_lines) or "<div>n/a</div>"
            status_value = str(position.get("status") or "")
            status_html = _fmt(status_value)
            if status_value.lower() == "partial":
                status_html += _tag("OUTSTANDING RISK", color="#b00020")
            if bool(position.get("simulated")) or status_value.lower() == "simulated":
                status_html += _tag("SIMULATED", color="#555", weight="600")
            parts.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    _fmt(position.get("symbol")),
                    status_html,
                    _fmt(position.get("notional_usdt")),
                    legs_html,
                    _fmt(position.get("unrealized_pnl_usdt")),
                )
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<p>No open hedge positions.</p>")

    parts.append("<h2>Background Health</h2><table><thead><tr><th>Component</th><th>Status</th><th>Detail</th></tr></thead><tbody>")
    for entry in health_checks:
        name = _fmt(entry.get("name"))
        ok = bool(entry.get("ok"))
        detail_value = entry.get("detail")
        detail = _fmt(detail_value)
        if not ok:
            detail = f"<span style=\"color:#b00020;font-weight:700;\">{detail or 'unavailable'}</span>"
        parts.append(
            f"<tr><td>{name}</td><td>{_status_span(ok)}</td><td>{detail}</td></tr>"
        )
    parts.append("</tbody></table>")

    parts.append("<h2>Pending Approvals</h2>")
    if approvals:
        parts.append(
            "<table><thead><tr><th>ID</th><th>Action</th><th>Requested By</th><th>Requested At</th><th>Status</th><th>Parameters</th></tr></thead><tbody>"
        )
        for entry in approvals:
            parts.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    _fmt(entry.get("id")),
                    _fmt(entry.get("action")),
                    _fmt(entry.get("requested_by")),
                    _fmt(entry.get("requested_ts")),
                    _fmt(entry.get("status")),
                    _fmt(entry.get("parameters")),
                )
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<p>No pending approvals.</p>")

    controls_disabled = not is_operator
    disabled_attr = " disabled" if controls_disabled else ""
    controls_parts = ["<div class=\"controls\"><h2>Controls</h2>"]
    if controls_disabled:
        controls_parts.append(
            "<p class=\"note\" style=\"color:#b91c1c;font-weight:600;\">Controls require operator role. Requests cannot be initiated from viewer accounts.</p>"
        )
    controls_parts.append(
        "<form method=\"post\" action=\"/api/ui/dashboard-hold\"><label for=\"hold-reason\">Trigger HOLD</label>"
        f"<input id=\"hold-reason\" name=\"reason\" type=\"text\" placeholder=\"reason (optional)\"{disabled_attr} />"
        "<label for=\"hold-operator\">Operator (optional)</label>"
        f"<input id=\"hold-operator\" name=\"operator\" type=\"text\" placeholder=\"who is requesting\"{disabled_attr} />"
        f"<button type=\"submit\"{disabled_attr}>Enable HOLD</button></form>"
    )
    controls_parts.append(
        "<form method=\"post\" action=\"/api/ui/dashboard-resume-request\"><label for=\"resume-reason\">Request RESUME</label>"
        f"<input id=\"resume-reason\" name=\"reason\" type=\"text\" placeholder=\"Why trading should resume\" required{disabled_attr} />"
        "<label for=\"resume-operator\">Operator (optional)</label>"
        f"<input id=\"resume-operator\" name=\"operator\" type=\"text\" placeholder=\"who is requesting\"{disabled_attr} />"
        "<div class=\"note\">Request is logged and still requires second-operator approval with APPROVE_TOKEN.</div>"
        f"<button type=\"submit\"{disabled_attr}>Request RESUME</button></form>"
    )
    controls_parts.append(
        "<form method=\"post\" action=\"/api/ui/dashboard-unfreeze-strategy\"><label for=\"unfreeze-strategy\">Unfreeze strategy</label>"
        f"<input id=\"unfreeze-strategy\" name=\"strategy\" type=\"text\" placeholder=\"strategy identifier\" required{disabled_attr} />"
        "<label for=\"unfreeze-reason\">Reason</label>"
        f"<input id=\"unfreeze-reason\" name=\"reason\" type=\"text\" placeholder=\"Why override is safe\" required{disabled_attr} />"
        "<div class=\"note\">Clears the risk freeze and resets consecutive failure counters. Audit trail is recorded.</div>"
        f"<button type=\"submit\"{disabled_attr}>Unfreeze strategy</button></form>"
    )
    controls_parts.append(
        "<form method=\"post\" action=\"/ui/dashboard/kill\"><label for=\"kill-operator\">Emergency Cancel All / Kill Switch</label>"
        f"<input id=\"kill-operator\" name=\"operator\" type=\"text\" placeholder=\"operator (optional)\"{disabled_attr} />"
        "<div class=\"note\">Invokes existing guarded endpoint to cancel managed orders immediately.</div>"
        f"<button type=\"submit\"{disabled_attr}>Emergency CANCEL ALL</button></form>"
    )
    controls_parts.append("</div>")
    parts.append("".join(controls_parts))

    build_value = _fmt(context.get("build_version")) or "n/a"
    last_snapshot_ts = ""
    if pnl_history:
        try:
            last_snapshot_ts = _fmt(pnl_history[0].get("timestamp"))
        except (TypeError, AttributeError, IndexError):
            last_snapshot_ts = ""
    if not last_snapshot_ts:
        last_snapshot_ts = "n/a"
    warning_text = "All trading actions require dual approval. Manual overrides are audited."
    parts.append(
        "<footer>Build version: <strong>{build}</strong> • Last PnL snapshot: {snapshot} • "
        "<span class=\"footer-warning\">{warning}</span></footer>".format(
            build=build_value,
            snapshot=last_snapshot_ts,
            warning=_fmt(warning_text),
        )
    )

    parts.append("</body></html>")
    return "".join(parts)


__all__ = ["build_dashboard_context", "render_dashboard_html"]

