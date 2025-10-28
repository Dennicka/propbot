from __future__ import annotations

import json
import os
from dataclasses import asdict
from html import escape
from typing import Any, Dict, Mapping, Sequence

from fastapi import Request

from ..opsbot import notifier
from ..runtime_state_store import load_runtime_payload
from ..version import APP_VERSION
from .approvals_store import list_requests as list_pending_requests
from . import risk_alerts, risk_guard
from .runtime import (
    get_auto_hedge_state,
    get_last_opportunity_state,
    get_state,
)
from .positions_view import build_positions_snapshot
from positions import list_positions
from pnl_history_store import list_recent as list_recent_snapshots
from services import adaptive_risk_advisor


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
    recent_audit = notifier.get_recent_alerts(limit=5)

    hold_reason = str(safety_payload.get("hold_reason") or "")
    risk_throttled = bool(
        safety_payload.get("hold_active")
        and hold_reason.upper().startswith(risk_guard.AUTO_THROTTLE_PREFIX)
    )

    pnl_history = list_recent_snapshots(limit=5)
    pnl_trend = _trend_summary(pnl_history)

    hold_info = {
        "hold_active": safety_payload.get("hold_active"),
        "hold_reason": safety_payload.get("hold_reason"),
        "hold_since": safety_payload.get("hold_since"),
        "last_released_ts": safety_payload.get("last_released_ts"),
    }
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
        "pending_approvals": approvals,
        "persisted_snapshot": persisted,
        "active_alerts": active_alerts,
        "recent_audit": recent_audit,
        "risk_throttled": risk_throttled,
        "risk_throttle_reason": hold_reason if risk_throttled else "",
        "pnl_history": pnl_history,
        "pnl_trend": pnl_trend,
        "risk_advice": risk_advice,
    }


def _bool_pill(flag: bool, *, true: str, false: str) -> str:
    return true if flag else false


def _fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return ("{:.6f}".format(value)).rstrip("0").rstrip(".") or "0"
    return escape(str(value))


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
    risk_limits_env = context.get("risk_limits_env", {}) or {}
    risk_state = context.get("risk_limits_state", {}) or {}
    exposures = context.get("exposure", {}) or {}
    positions = context.get("positions", []) or []
    totals = context.get("position_totals", {}) or {}
    health_checks = context.get("health_checks", []) or []
    approvals = context.get("pending_approvals", []) or []
    flash_messages = context.get("flash_messages", []) or []
    risk_throttled = bool(context.get("risk_throttled"))
    throttle_reason = context.get("risk_throttle_reason") or ""
    active_alerts = context.get("active_alerts", []) or []
    recent_audit = context.get("recent_audit", []) or []
    trend = context.get("pnl_trend", {}) or {}

    risk_advice = context.get("risk_advice", {}) or {}

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
        "</style></head><body>"
    )
    parts.append(
        f"<h1>Operator Dashboard</h1><p>Build Version: <strong>{_fmt(context.get('build_version'))}</strong></p>"
    )

    for message in flash_messages:
        parts.append(f"<div class=\"flash\">{_fmt(message)}</div>")

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
    hold_reason = safety.get("hold_reason")
    hold_since = safety.get("hold_since")
    parts.append("<h2>Runtime &amp; Safety</h2><table><tbody>")
    mode_value = _fmt(context.get("control", {}).get("mode"))
    if risk_throttled:
        mode_value = f"RISK_THROTTLED ({mode_value})"
    parts.append(f"<tr><th>Mode</th><td>{mode_value}</td></tr>")
    if hold_active:
        detail = "YES"
        if hold_reason:
            detail += f" - Reason: {_fmt(hold_reason)}"
        if hold_since:
            detail += f" (since {_fmt(hold_since)})"
        hold_cell = f'<span style="color:#b00020;font-weight:700;">{detail}</span>'
    else:
        hold_cell = '<span style=\"color:#1b7f3b;font-weight:600;\">NO</span>'
    parts.append(f"<tr><th>HOLD Active</th><td>{hold_cell}</td></tr>")
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

    parts.append("<h2>Risk Limits</h2><table><thead><tr><th>Limit</th><th>Configured Value</th></tr></thead><tbody>")
    for name, value in sorted(risk_limits_env.items()):
        parts.append(f"<tr><td>{_fmt(name)}</td><td>{_fmt(value)}</td></tr>")
    parts.append("</tbody></table>")
    parts.append(f"<p class=\"note\">Runtime risk limits snapshot: {_fmt(risk_state)}</p>")

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

    parts.append(
        "<div class=\"controls\"><h2>Controls</h2>"
        "<form method=\"post\" action=\"/ui/dashboard/hold\"><label for=\"hold-reason\">Trigger HOLD</label>"
        "<input id=\"hold-reason\" name=\"reason\" type=\"text\" placeholder=\"reason (optional)\" />"
        "<label for=\"hold-operator\">Operator (optional)</label>"
        "<input id=\"hold-operator\" name=\"operator\" type=\"text\" placeholder=\"who is requesting\" />"
        "<button type=\"submit\">Enable HOLD</button></form>"
        "<form method=\"post\" action=\"/ui/dashboard/resume\"><label for=\"resume-reason\">Request RESUME</label>"
        "<input id=\"resume-reason\" name=\"reason\" type=\"text\" placeholder=\"Why trading should resume\" required />"
        "<label for=\"resume-operator\">Operator (optional)</label>"
        "<input id=\"resume-operator\" name=\"operator\" type=\"text\" placeholder=\"who is requesting\" />"
        "<div class=\"note\">Request is logged and still requires second-operator approval with APPROVE_TOKEN.</div>"
        "<button type=\"submit\">Request RESUME</button></form>"
        "<form method=\"post\" action=\"/ui/dashboard/kill\"><label for=\"kill-operator\">Emergency Cancel All / Kill Switch</label>"
        "<input id=\"kill-operator\" name=\"operator\" type=\"text\" placeholder=\"operator (optional)\" />"
        "<div class=\"note\">Invokes existing guarded endpoint to cancel managed orders immediately.</div>"
        "<button type=\"submit\">Emergency CANCEL ALL</button></form></div>"
    )

    parts.append("</body></html>")
    return "".join(parts)


__all__ = ["build_dashboard_context", "render_dashboard_html"]

