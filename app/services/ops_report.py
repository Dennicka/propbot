"""Builders for the operations risk report payloads."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from positions import list_positions

from ..audit_log import list_recent_operator_actions
from ..pnl_report import build_pnl_snapshot
from ..strategy_budget import get_strategy_budget_manager
from ..strategy_pnl import snapshot_all as snapshot_strategy_pnl
from ..strategy_risk import get_strategy_risk_manager
from . import runtime
from .audit_log import list_recent_events
from .positions_view import build_positions_snapshot
from .strategy_status import build_strategy_status


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stringify(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _coerce_mapping(payload: object) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    return {}


def _normalise_strategy_controls(raw_snapshot: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_snapshot, Mapping):
        return {}
    strategies = raw_snapshot.get("strategies")
    if not isinstance(strategies, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(strategies):
        entry = strategies.get(name)
        if not isinstance(entry, Mapping):
            continue
        state = entry.get("state") if isinstance(entry.get("state"), Mapping) else {}
        freeze_reason = ""
        if isinstance(state, Mapping) and state:
            freeze_reason = str(state.get("freeze_reason") or state.get("reason") or "")
        else:
            freeze_reason = str(entry.get("freeze_reason") or entry.get("reason") or "")
        limits = entry.get("limits") if isinstance(entry.get("limits"), Mapping) else {}
        breach_reasons = [
            str(reason)
            for reason in entry.get("breach_reasons", [])
            if isinstance(reason, (str, int, float)) or reason
        ]
        result[str(name)] = {
            "enabled": bool(entry.get("enabled")),
            "frozen": bool(entry.get("frozen")),
            "freeze_reason": freeze_reason,
            "breach": bool(entry.get("breach")),
            "breach_reasons": breach_reasons,
            "limits": {str(k): v for k, v in _coerce_mapping(limits).items()},
        }
    return result


def _build_per_strategy_pnl(
    strategy_snapshot: Mapping[str, Any],
    strategy_budget_snapshot: Mapping[str, Mapping[str, Any]],
    strategy_pnl_snapshot: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    strategies = strategy_snapshot.get("strategies")
    strategies_mapping = strategies if isinstance(strategies, Mapping) else {}
    names = set(strategies_mapping) | set(strategy_budget_snapshot) | set(strategy_pnl_snapshot)
    result: dict[str, dict[str, Any]] = {}
    for name in sorted(names):
        risk_entry = strategies_mapping.get(name)
        if isinstance(risk_entry, Mapping):
            state = risk_entry.get("state") if isinstance(risk_entry.get("state"), Mapping) else {}
            frozen = bool(state.get("frozen") or risk_entry.get("frozen"))
        else:
            state = {}
            frozen = False
        pnl_entry = strategy_pnl_snapshot.get(name) if isinstance(strategy_pnl_snapshot.get(name), Mapping) else {}
        budget_entry = strategy_budget_snapshot.get(name) if isinstance(strategy_budget_snapshot.get(name), Mapping) else {}
        result[name] = {
            "realized_pnl_today": pnl_entry.get("realized_pnl_today", 0.0),
            "realized_pnl_total": pnl_entry.get("realized_pnl_total", 0.0),
            "realized_pnl_7d": pnl_entry.get("realized_pnl_7d", 0.0),
            "max_drawdown_observed": pnl_entry.get("max_drawdown_observed", 0.0),
            "frozen": frozen,
            "budget_blocked": bool(budget_entry.get("blocked")),
        }
    return result


async def build_ops_report(*, actions_limit: int = 10, events_limit: int = 10) -> dict[str, Any]:
    """Assemble the structured operations report for API consumers."""

    state = runtime.get_state()
    positions = list_positions()
    positions_snapshot = await build_positions_snapshot(state, positions)
    pnl_snapshot = build_pnl_snapshot(positions_snapshot)
    strategy_snapshot = get_strategy_risk_manager().full_snapshot()
    strategy_budget_snapshot = get_strategy_budget_manager().snapshot()
    strategy_pnl_snapshot = snapshot_strategy_pnl()
    strategy_status_snapshot = build_strategy_status()

    control = state.control
    autopilot = state.autopilot.as_dict() if hasattr(state.autopilot, "as_dict") else {}
    safety = state.safety.as_dict() if hasattr(state.safety, "as_dict") else {}

    operator_actions = list_recent_operator_actions(limit=max(actions_limit, 0))
    ops_events = list_recent_events(limit=max(events_limit, 0))

    return {
        "generated_at": _iso_now(),
        "runtime": {
            "mode": control.mode,
            "safe_mode": control.safe_mode,
            "dry_run": control.dry_run,
            "dry_run_mode": getattr(control, "dry_run_mode", False),
            "two_man_rule": getattr(control, "two_man_rule", True),
            "flags": dict(control.flags),
            "autopilot": autopilot,
            "safety": {
                "hold_active": safety.get("hold_active"),
                "hold_reason": safety.get("hold_reason"),
                "hold_source": safety.get("hold_source"),
                "hold_since": safety.get("hold_since"),
                "last_released_ts": safety.get("last_released_ts"),
                "resume_request": safety.get("resume_request"),
            },
        },
        "autopilot": autopilot,
        "pnl": pnl_snapshot,
        "positions_snapshot": {
            "positions": list(positions_snapshot.get("positions", [])),
            "exposure": {str(k): v for k, v in _coerce_mapping(positions_snapshot.get("exposure")).items()},
            "totals": {str(k): v for k, v in _coerce_mapping(positions_snapshot.get("totals")).items()},
        },
        "strategy_controls": _normalise_strategy_controls(strategy_snapshot),
        "per_strategy_pnl": _build_per_strategy_pnl(
            strategy_snapshot,
            strategy_budget_snapshot,
            strategy_pnl_snapshot,
        ),
        "strategy_status": strategy_status_snapshot,
        "strategy_budgets": strategy_budget_snapshot,
        "audit": {
            "operator_actions": operator_actions,
            "ops_events": ops_events,
        },
    }


def _iter_runtime_rows(runtime_payload: Mapping[str, Any]) -> Iterable[dict[str, str]]:
    for key in ("mode", "safe_mode", "dry_run", "dry_run_mode", "two_man_rule"):
        yield {"section": "runtime", "key": key, "value": _stringify(runtime_payload.get(key))}
    autopilot = _coerce_mapping(runtime_payload.get("autopilot"))
    for key in ("enabled", "target_mode", "target_safe_mode", "last_action", "last_reason"):
        if key in autopilot:
            yield {"section": "autopilot", "key": key, "value": _stringify(autopilot.get(key))}
    safety = _coerce_mapping(runtime_payload.get("safety"))
    for key in ("hold_active", "hold_reason", "hold_since", "hold_source", "last_released_ts"):
        if key in safety:
            yield {"section": "safety", "key": key, "value": _stringify(safety.get(key))}


def _iter_strategy_rows(strategies: Mapping[str, Mapping[str, Any]]) -> Iterable[dict[str, str]]:
    for name in sorted(strategies):
        payload = _coerce_mapping(strategies.get(name))
        section = f"strategy:{name}"
        for key in ("enabled", "frozen", "freeze_reason", "breach"):
            if key in payload:
                yield {"section": section, "key": key, "value": _stringify(payload.get(key))}
        reasons = payload.get("breach_reasons")
        if isinstance(reasons, Sequence):
            for index, reason in enumerate(reasons, start=1):
                yield {
                    "section": section,
                    "key": f"breach_reason_{index}",
                    "value": _stringify(reason),
                }


def _iter_strategy_budget_rows(budgets: Mapping[str, Mapping[str, Any]]) -> Iterable[dict[str, str]]:
    for name in sorted(budgets):
        entry = _coerce_mapping(budgets.get(name))
        section = f"strategy_budget:{name}"
        for key in (
            "current_notional_usdt",
            "max_notional_usdt",
            "current_open_positions",
            "max_open_positions",
            "blocked",
        ):
            if key in entry:
                yield {
                    "section": section,
                    "key": key,
                    "value": _stringify(entry.get(key)),
                }


def _iter_strategy_status_rows(statuses: Mapping[str, Mapping[str, Any]]) -> Iterable[dict[str, str]]:
    for name in sorted(statuses):
        entry = _coerce_mapping(statuses.get(name))
        section = f"strategy_status:{name}"
        for key in (
            "enabled",
            "frozen",
            "freeze_reason",
            "last_breach",
            "consecutive_failures",
            "realized_pnl_today",
            "realized_pnl_total",
            "budget_blocked",
            "max_drawdown_observed",
        ):
            if key in entry:
                yield {
                    "section": section,
                    "key": key,
                    "value": _stringify(entry.get(key)),
                }


def _iter_strategy_pnl_rows(payload: Mapping[str, Any]) -> Iterable[dict[str, str]]:
    for name in sorted(payload):
        entry = _coerce_mapping(payload.get(name))
        section = f"strategy_pnl:{name}"
        for key in (
            "realized_pnl_today",
            "realized_pnl_total",
            "realized_pnl_7d",
            "max_drawdown_observed",
            "frozen",
            "budget_blocked",
        ):
            if key in entry:
                yield {
                    "section": section,
                    "key": key,
                    "value": _stringify(entry.get(key)),
                }


def _iter_pnl_rows(pnl_payload: Mapping[str, Any]) -> Iterable[dict[str, str]]:
    for key in ("unrealized_pnl_usdt", "realised_pnl_today_usdt", "total_exposure_usdt"):
        if key in pnl_payload:
            yield {"section": "pnl", "key": key, "value": _stringify(pnl_payload.get(key))}


def _iter_positions_rows(positions_payload: Mapping[str, Any]) -> Iterable[dict[str, str]]:
    totals = _coerce_mapping(positions_payload.get("totals"))
    for key in sorted(totals):
        yield {
            "section": "positions_totals",
            "key": key,
            "value": _stringify(totals.get(key)),
        }
    exposure = _coerce_mapping(positions_payload.get("exposure"))
    for venue in sorted(exposure):
        venue_payload = _coerce_mapping(exposure.get(venue))
        section = f"exposure:{venue}"
        for key in ("long_notional", "short_notional", "net_usdt"):
            if key in venue_payload:
                yield {
                    "section": section,
                    "key": key,
                    "value": _stringify(venue_payload.get(key)),
                }


def _iter_audit_rows(audit_payload: Mapping[str, Any]) -> Iterable[dict[str, str]]:
    actions = audit_payload.get("operator_actions")
    if isinstance(actions, Sequence):
        for index, action in enumerate(actions, start=1):
            yield {
                "section": "operator_actions",
                "key": f"{index:02d}",
                "value": _stringify(action),
            }
    events = audit_payload.get("ops_events")
    if isinstance(events, Sequence):
        for index, event in enumerate(events, start=1):
            yield {
                "section": "ops_events",
                "key": f"{index:02d}",
                "value": _stringify(event),
            }


def build_ops_report_csv(report: Mapping[str, Any]) -> str:
    """Render ``report`` as a stable CSV document."""

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=["section", "key", "value"])
    writer.writeheader()
    generated_at = report.get("generated_at")
    if generated_at:
        writer.writerow({"section": "metadata", "key": "generated_at", "value": _stringify(generated_at)})
    runtime_payload = _coerce_mapping(report.get("runtime"))
    for row in _iter_runtime_rows(runtime_payload):
        writer.writerow(row)
    strategy_payload = _coerce_mapping(report.get("strategy_controls"))
    for row in _iter_strategy_rows(strategy_payload):
        writer.writerow(row)
    budget_payload = _coerce_mapping(report.get("strategy_budgets"))
    for row in _iter_strategy_budget_rows(budget_payload):
        writer.writerow(row)
    status_payload = _coerce_mapping(report.get("strategy_status"))
    for row in _iter_strategy_status_rows(status_payload):
        writer.writerow(row)
    per_strategy_pnl_payload = _coerce_mapping(report.get("per_strategy_pnl"))
    for row in _iter_strategy_pnl_rows(per_strategy_pnl_payload):
        writer.writerow(row)
    pnl_payload = _coerce_mapping(report.get("pnl"))
    for row in _iter_pnl_rows(pnl_payload):
        writer.writerow(row)
    positions_payload = _coerce_mapping(report.get("positions_snapshot"))
    for row in _iter_positions_rows(positions_payload):
        writer.writerow(row)
    audit_payload = _coerce_mapping(report.get("audit"))
    for row in _iter_audit_rows(audit_payload):
        writer.writerow(row)
    return buffer.getvalue()


__all__ = ["build_ops_report", "build_ops_report_csv"]
