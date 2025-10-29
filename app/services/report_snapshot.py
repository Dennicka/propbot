"""Helpers to build operator-facing risk snapshots."""
from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from positions import list_positions

from ..strategy_risk import get_strategy_risk_manager
from .positions_view import build_positions_snapshot
from .runtime import get_state
from ..version import APP_VERSION
from services.audit_snapshot import get_recent_audit_snapshot

_CRITICAL_PREFIXES: Sequence[str] = (
    "HOLD",
    "RESUME",
    "KILL",
    "STRATEGY",
    "UNFREEZE",
    "FREEZE",
    "AUTOPILOT",
    "DRY_RUN",
    "CANCEL",
    "RAISE_LIMIT",
    "EXIT_DRY_RUN",
    "REPORT",
    "RESUME_REQUEST",
)


async def build_ops_report_snapshot(recent_actions_limit: int = 10) -> Dict[str, Any]:
    """Assemble an aggregated operational risk snapshot."""

    state = get_state()
    timestamp = datetime.now(timezone.utc).isoformat()
    build_version = os.getenv("BUILD_VERSION") or APP_VERSION
    safety = state.safety
    control = state.control

    positions = list_positions()
    exposure: Dict[str, Dict[str, float]] = {}
    pnl_summary: Dict[str, Any] = {"unrealized_pnl_usdt": 0.0}
    open_hedges: List[Dict[str, Any]] = []
    open_count = 0
    partial_count = 0

    if positions:
        snapshot = await build_positions_snapshot(state, positions)
        exposure = _coerce_exposure(snapshot.get("exposure", {}))
        pnl_summary = dict(snapshot.get("totals", {}) or {})
        for entry in snapshot.get("positions", []):
            status = str(entry.get("status") or "").lower()
            if status not in {"open", "partial"}:
                continue
            hedge_payload = {
                "id": entry.get("id"),
                "symbol": entry.get("symbol"),
                "notional_usdt": entry.get("notional_usdt"),
                "status": status,
                "long_venue": entry.get("long_venue"),
                "short_venue": entry.get("short_venue"),
                "unrealized_pnl_usdt": entry.get("unrealized_pnl_usdt"),
                "legs": entry.get("legs", []),
            }
            open_hedges.append(hedge_payload)
            open_count += 1
            if status == "partial":
                partial_count += 1

    exposure_totals = _aggregate_exposure_totals(exposure)

    manager = get_strategy_risk_manager()
    strategy_snapshot = manager.full_snapshot()
    strategies = _render_strategy_status(strategy_snapshot.get("strategies", {}))

    recent_entries = get_recent_audit_snapshot(limit=max(recent_actions_limit * 2, recent_actions_limit))
    recent_actions = _select_recent_actions(recent_entries, limit=recent_actions_limit)

    report = {
        "timestamp": timestamp,
        "build_version": build_version,
        "mode": control.mode,
        "global_flags": {
            "SAFE_MODE": bool(control.safe_mode),
            "HOLD": bool(safety.hold_active),
            "DRY_RUN_MODE": bool(getattr(control, "dry_run_mode", False)),
            "AUTOPILOT_ENABLE": bool(state.autopilot.enabled),
        },
        "hold_details": {
            "active": bool(safety.hold_active),
            "reason": safety.hold_reason,
            "since": safety.hold_since,
            "source": safety.hold_source,
        },
        "strategies": strategies,
        "exposure": exposure,
        "exposure_totals": exposure_totals,
        "pnl_summary": pnl_summary,
        "open_hedges": open_hedges,
        "open_hedges_count": open_count,
        "partial_hedges_count": partial_count,
        "recent_actions": recent_actions,
    }
    return report


def render_ops_report_csv(report: Mapping[str, Any]) -> str:
    """Render ``report`` into a simple section/key/value CSV string."""

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["section", "key", "value"])

    def _write(section: str, key: str, value: Any) -> None:
        writer.writerow([section, key, _serialise_value(value)])

    for key, value in report.items():
        if isinstance(value, Mapping):
            for sub_key, sub_value in value.items():
                _write(key, str(sub_key), sub_value)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                label = f"{key}[{index}]"
                _write(key, label, item)
        else:
            _write("meta", str(key), value)

    return buffer.getvalue()


def _serialise_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _coerce_exposure(payload: Mapping[str, Any]) -> Dict[str, Dict[str, float]]:
    exposure: Dict[str, Dict[str, float]] = {}
    for venue, stats in payload.items():
        if not isinstance(stats, Mapping):
            continue
        try:
            long_notional = float(stats.get("long_notional", 0.0))
            short_notional = float(stats.get("short_notional", 0.0))
            net_usdt = float(stats.get("net_usdt", 0.0))
        except (TypeError, ValueError):
            long_notional = short_notional = net_usdt = 0.0
        exposure[str(venue)] = {
            "long_notional": long_notional,
            "short_notional": short_notional,
            "net_usdt": net_usdt,
        }
    return exposure


def _aggregate_exposure_totals(exposure: Mapping[str, Mapping[str, float]]) -> Dict[str, float]:
    long_total = 0.0
    short_total = 0.0
    net_total = 0.0
    for stats in exposure.values():
        try:
            long_total += float(stats.get("long_notional", 0.0))
            short_total += float(stats.get("short_notional", 0.0))
            net_total += float(stats.get("net_usdt", 0.0))
        except (TypeError, ValueError):
            continue
    return {
        "long_notional": long_total,
        "short_notional": short_total,
        "net_usdt": net_total,
    }


def _render_strategy_status(strategies: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rendered: List[Dict[str, Any]] = []
    for name in sorted(strategies):
        entry = strategies.get(name) or {}
        state_payload = entry.get("state", {}) or {}
        reason = state_payload.get("freeze_reason") or state_payload.get("reason") or ""
        rendered.append(
            {
                "name": name,
                "enabled": bool(entry.get("enabled", state_payload.get("enabled", True))),
                "frozen": bool(entry.get("frozen", state_payload.get("frozen", False))),
                "reason": reason,
            }
        )
    return rendered


def _select_recent_actions(entries: Iterable[Mapping[str, Any]], limit: int) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    for entry in reversed(list(entries)):
        action = str(entry.get("action") or "")
        action_upper = action.upper()
        if any(action_upper.startswith(prefix) for prefix in _CRITICAL_PREFIXES):
            selected.append(_simplify_action(entry))
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for entry in reversed(list(entries)):
            simplified = _simplify_action(entry)
            if simplified in selected:
                continue
            selected.append(simplified)
            if len(selected) >= limit:
                break
    return selected[:limit]


def _simplify_action(entry: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "timestamp": entry.get("timestamp"),
        "operator": entry.get("operator_name"),
        "role": entry.get("role"),
        "action": entry.get("action"),
        "details": entry.get("details"),
    }


__all__ = ["build_ops_report_snapshot", "render_ops_report_csv"]
