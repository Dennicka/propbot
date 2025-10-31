"""Builders for the operations risk report payloads."""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from positions import list_positions

from ..audit_log import list_recent_operator_actions
from ..pnl_report import build_pnl_snapshot
from ..strategy_budget import get_strategy_budget_manager
from ..strategy.pnl_tracker import get_strategy_pnl_tracker
from ..strategy_pnl import snapshot_all as snapshot_strategy_pnl
from ..strategy_risk import get_strategy_risk_manager
from ..watchdog.exchange_watchdog import get_exchange_watchdog
from ..universe.gate import is_universe_enforced
from . import runtime
from .runtime_badges import get_runtime_badges
from .audit_log import list_recent_events
from .positions_view import build_positions_snapshot
from .strategy_status import build_strategy_status
from ..risk.daily_loss import get_daily_loss_cap_state
from ..risk.accounting import get_risk_snapshot as get_risk_accounting_snapshot


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


def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _count_open_trades(positions_snapshot: Mapping[str, Any]) -> int:
    positions = positions_snapshot.get("positions")
    if not isinstance(positions, Sequence):
        return 0
    count = 0
    for entry in positions:
        if not isinstance(entry, Mapping):
            continue
        status = str(entry.get("status") or "").strip().lower()
        if status in {"", "open", "partial", "opening"}:
            count += 1
            continue
        legs = entry.get("legs")
        if isinstance(legs, Sequence):
            if any(str(leg.get("status") or "").strip().lower() in {"open", "partial"} for leg in legs if isinstance(leg, Mapping)):
                count += 1
    return count


def _normalise_audit_action(entry: Mapping[str, Any]) -> dict[str, Any]:
    timestamp = str(entry.get("timestamp") or entry.get("ts") or "")
    operator = str(entry.get("operator") or entry.get("operator_name") or "")
    role = str(entry.get("role") or "")
    action = str(entry.get("action") or "")
    details = entry.get("details")
    if isinstance(details, Mapping):
        details_payload: Any = dict(details)
    else:
        details_payload = details
    return {
        "ts": timestamp,
        "operator": operator,
        "role": role,
        "action": action,
        "details": details_payload,
    }


def _extract_budget_rows(accounting_snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    budgets: list[dict[str, Any]] = []
    per_strategy = accounting_snapshot.get("per_strategy")
    if not isinstance(per_strategy, Mapping):
        return budgets
    for strategy in sorted(per_strategy):
        entry = per_strategy.get(strategy)
        entry_mapping = entry if isinstance(entry, Mapping) else {}
        budget_info = entry_mapping.get("budget")
        budget_mapping = budget_info if isinstance(budget_info, Mapping) else {}
        limit_value = budget_mapping.get("limit_usdt")
        used_value = budget_mapping.get("used_today_usdt")
        remaining_value = budget_mapping.get("remaining_usdt")
        try:
            limit_float = float(limit_value) if limit_value is not None else None
        except (TypeError, ValueError):
            limit_float = None
        try:
            used_float = float(used_value) if used_value is not None else 0.0
        except (TypeError, ValueError):
            used_float = 0.0
        try:
            remaining_float = float(remaining_value) if remaining_value is not None else None
        except (TypeError, ValueError):
            remaining_float = None if limit_float is None else limit_float - used_float
        budgets.append(
            {
                "strategy": str(strategy),
                "budget_usdt": limit_float,
                "used_usdt": used_float,
                "remaining_usdt": remaining_float,
            }
        )
    return budgets


def _resolve_daily_loss_badge(report: Mapping[str, Any]) -> str:
    badges = report.get("badges")
    if isinstance(badges, Mapping):
        badge = badges.get("daily_loss")
        if badge is not None:
            return str(badge)
    return ""


def _resolve_watchdog_badge(report: Mapping[str, Any]) -> str:
    badges = report.get("badges")
    if isinstance(badges, Mapping):
        badge = badges.get("watchdog")
        if badge is not None:
            return str(badge)
    return ""


def _resolve_auto_trade_badge(report: Mapping[str, Any]) -> str:
    badges = report.get("badges")
    if isinstance(badges, Mapping):
        badge = badges.get("auto_trade")
        if badge is not None:
            return str(badge)
    return ""


def _iter_budget_rows(report: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    budgets = report.get("budgets")
    if isinstance(budgets, Sequence) and budgets:
        for entry in budgets:
            if isinstance(entry, Mapping):
                yield entry
        return
    yield {}


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
    pnl_tracker = get_strategy_pnl_tracker()
    tracker_snapshot = pnl_tracker.snapshot()
    tracker_rows = [
        {
            "name": name,
            "realized_today": float(entry.get("realized_today", 0.0)),
            "realized_7d": float(entry.get("realized_7d", 0.0)),
            "max_drawdown_7d": float(entry.get("max_drawdown_7d", 0.0)),
        }
        for name, entry in tracker_snapshot.items()
    ]
    tracker_rows.sort(key=lambda row: row["realized_today"])
    strategy_pnl_tracker_payload = {
        "strategies": tracker_rows,
        "simulated_excluded": pnl_tracker.exclude_simulated_entries(),
    }
    strategy_status_snapshot = build_strategy_status()
    watchdog_instance = get_exchange_watchdog()
    watchdog_snapshot = watchdog_instance.get_state()
    watchdog_recent = watchdog_instance.get_recent_transitions(window_minutes=50)
    degraded_reasons = {}
    if isinstance(watchdog_snapshot, Mapping):
        for name, entry in watchdog_snapshot.items():
            payload = entry if isinstance(entry, Mapping) else {}
            if not bool(payload.get("ok", False)):
                degraded_reasons[str(name)] = str(payload.get("reason") or "")
    watchdog_report = {
        "overall_ok": watchdog_instance.overall_ok(),
        "watchdog_ok": watchdog_instance.overall_ok(),
        "exchanges": watchdog_snapshot,
        "degraded_reasons": degraded_reasons,
        "recent_transitions": watchdog_recent,
    }
    daily_loss_cap_snapshot = get_daily_loss_cap_state()

    control = state.control
    autopilot = state.autopilot.as_dict() if hasattr(state.autopilot, "as_dict") else {}
    safety = state.safety.as_dict() if hasattr(state.safety, "as_dict") else {}

    operator_actions = list_recent_operator_actions(limit=max(actions_limit, 0))
    ops_events = list_recent_events(limit=max(events_limit, 0))
    accounting_snapshot = get_risk_accounting_snapshot()
    accounting_mapping = accounting_snapshot if isinstance(accounting_snapshot, Mapping) else {}
    budgets_payload = _extract_budget_rows(accounting_mapping)
    normalised_actions = [
        _normalise_audit_action(entry)
        for entry in operator_actions
        if isinstance(entry, Mapping)
    ]
    universe_enforced = is_universe_enforced()
    unknown_pairs = runtime.get_universe_unknown_pairs()
    max_open_trades_limit = _env_int("MAX_OPEN_POSITIONS", 0)
    open_trades_count = _count_open_trades(positions_snapshot)

    return {
        "generated_at": _iso_now(),
        "open_trades_count": open_trades_count,
        "max_open_trades_limit": max_open_trades_limit,
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
        "badges": get_runtime_badges(),
        "autopilot": autopilot,
        "pnl": pnl_snapshot,
        "daily_loss_cap": daily_loss_cap_snapshot,
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
        "strategy_pnl": strategy_pnl_tracker_payload,
        "strategy_budgets": strategy_budget_snapshot,
        "watchdog": watchdog_report,
        "audit": {
            "operator_actions": operator_actions,
            "ops_events": ops_events,
        },
        "last_audit_actions": normalised_actions[-10:],
        "budgets": budgets_payload,
        "universe_enforced": universe_enforced,
        "unknown_pairs": list(unknown_pairs),
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


def _iter_strategy_pnl_tracker_rows(payload: Mapping[str, Any]) -> Iterable[dict[str, str]]:
    simulated_flag = payload.get("simulated_excluded")
    if simulated_flag is not None:
        yield {
            "section": "strategy_pnl",
            "key": "simulated_excluded",
            "value": _stringify(simulated_flag),
        }
    strategies = payload.get("strategies")
    if isinstance(strategies, Sequence):
        for entry in strategies:
            if not isinstance(entry, Mapping):
                continue
            name = str(entry.get("name") or "")
            if not name:
                continue
            section = f"strategy_pnl:{name}"
            for key in ("realized_today", "realized_7d", "max_drawdown_7d"):
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


def _iter_daily_loss_rows(snapshot: Mapping[str, Any]) -> Iterable[dict[str, str]]:
    payload = _coerce_mapping(snapshot)
    if not payload:
        return []
    for key in (
        "realized_pnl_today_usdt",
        "losses_usdt",
        "max_daily_loss_usdt",
        "remaining_usdt",
        "percentage_used",
        "breached",
        "enabled",
        "blocking",
    ):
        if key in payload:
            yield {
                "section": "daily_loss_cap",
                "key": key,
                "value": _stringify(payload.get(key)),
            }


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


def _iter_watchdog_rows(snapshot: Mapping[str, Any]) -> Iterable[dict[str, str]]:
    overall = snapshot.get("overall_ok")
    if overall is not None:
        yield {
            "section": "watchdog",
            "key": "overall_ok",
            "value": _stringify(overall),
        }
    if "watchdog_ok" in snapshot:
        yield {
            "section": "watchdog",
            "key": "watchdog_ok",
            "value": _stringify(snapshot.get("watchdog_ok")),
        }
    degraded = snapshot.get("degraded_reasons")
    if isinstance(degraded, Mapping):
        for name in sorted(degraded):
            yield {
                "section": "watchdog_degraded",
                "key": str(name),
                "value": _stringify(degraded.get(name)),
            }
    exchanges = _coerce_mapping(snapshot.get("exchanges"))
    for name in sorted(exchanges):
        entry = _coerce_mapping(exchanges.get(name))
        section = f"watchdog:{name}"
        if "ok" in entry:
            yield {
                "section": section,
                "key": "ok",
                "value": _stringify(entry.get("ok")),
            }
        if "last_check_ts" in entry:
            yield {
                "section": section,
                "key": "last_check_ts",
                "value": _stringify(entry.get("last_check_ts")),
            }
        if "reason" in entry:
            yield {
                "section": section,
                "key": "reason",
                "value": _stringify(entry.get("reason")),
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
    fieldnames = [
        "timestamp",
        "open_trades_count",
        "max_open_trades_limit",
        "daily_loss_status",
        "watchdog_status",
        "auto_trade",
        "strategy",
        "budget_usdt",
        "used_usdt",
        "remaining_usdt",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()

    timestamp = str(report.get("generated_at") or "")
    open_trades_count = report.get("open_trades_count")
    max_open_trades_limit = report.get("max_open_trades_limit")
    daily_loss_status = _resolve_daily_loss_badge(report)
    watchdog_status = _resolve_watchdog_badge(report)
    auto_trade_status = _resolve_auto_trade_badge(report)

    for budget in _iter_budget_rows(report):
        strategy = str(budget.get("strategy") or "") if budget else ""
        budget_usdt = budget.get("budget_usdt") if isinstance(budget, Mapping) else None
        used_usdt = budget.get("used_usdt") if isinstance(budget, Mapping) else None
        remaining_usdt = budget.get("remaining_usdt") if isinstance(budget, Mapping) else None
        writer.writerow(
            {
                "timestamp": timestamp,
                "open_trades_count": open_trades_count,
                "max_open_trades_limit": max_open_trades_limit,
                "daily_loss_status": daily_loss_status,
                "watchdog_status": watchdog_status,
                "auto_trade": auto_trade_status,
                "strategy": strategy,
                "budget_usdt": budget_usdt,
                "used_usdt": used_usdt,
                "remaining_usdt": remaining_usdt,
            }
        )

    return buffer.getvalue()


__all__ = ["build_ops_report", "build_ops_report_csv"]
