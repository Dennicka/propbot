"""Evaluate risk/health anomalies and surface persistent alerts."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Mapping, MutableMapping

from positions_store import list_records as list_position_records

from ..opsbot import notifier
from . import risk_guard
from .runtime import get_auto_hedge_state, get_state

LOGGER = logging.getLogger(__name__)

_ACTIVE_ALERTS: Dict[str, "_AlertState"] = {}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_ts(raw: object) -> datetime | None:
    if raw in (None, ""):
        return None
    text = str(raw)
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass
class _AlertState:
    alert_id: str
    kind: str
    text: str
    extra: Dict[str, object] | None
    active_since: datetime
    last_seen: datetime

    def as_payload(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "id": self.alert_id,
            "kind": self.kind,
            "text": self.text,
            "active_since": self.active_since.replace(tzinfo=timezone.utc).isoformat(),
            "last_seen": self.last_seen.replace(tzinfo=timezone.utc).isoformat(),
        }
        if self.extra:
            payload["extra"] = json.loads(json.dumps(self.extra, default=str))
        return payload


def _activate_alert(
    *,
    alert_id: str,
    kind: str,
    text: str,
    extra: Mapping[str, object] | None,
    now: datetime,
) -> None:
    existing = _ACTIVE_ALERTS.get(alert_id)
    payload_extra = dict(extra) if isinstance(extra, Mapping) else None
    if existing:
        existing.text = text
        existing.extra = payload_extra
        existing.last_seen = now
        return
    notifier.emit_alert(
        kind,
        text,
        extra=payload_extra or None,
        active=True,
        alert_id=alert_id,
    )
    _ACTIVE_ALERTS[alert_id] = _AlertState(
        alert_id=alert_id,
        kind=kind,
        text=text,
        extra=payload_extra,
        active_since=now,
        last_seen=now,
    )


def _resolve_alert(alert_id: str, *, now: datetime) -> None:
    state = _ACTIVE_ALERTS.pop(alert_id, None)
    if not state:
        return
    extra = dict(state.extra or {})
    extra.update({"resolved_at": now.replace(tzinfo=timezone.utc).isoformat()})
    notifier.emit_alert(
        f"{state.kind}_resolved",
        f"Resolved alert: {state.text}",
        extra=extra,
        active=False,
        alert_id=state.alert_id,
    )


def _partial_hedge_alerts(
    now: datetime, *, threshold: float
) -> Iterable[tuple[str, str, str, Dict[str, object]]]:
    if threshold <= 0:
        return []
    alerts: list[tuple[str, str, str, Dict[str, object]]] = []
    for record in list_position_records():
        status = str(record.get("status") or "").lower()
        if status not in {"partial"}:
            continue
        if bool(record.get("simulated")):
            continue
        opened_at = _parse_ts(record.get("timestamp"))
        if not opened_at:
            continue
        age = (now - opened_at).total_seconds()
        if age < threshold:
            continue
        legs = record.get("legs")
        if isinstance(legs, list):
            incomplete = False
            for leg in legs:
                if not isinstance(leg, Mapping):
                    continue
                leg_status = str(leg.get("status") or "").lower()
                if leg_status in {"closed", "filled"}:
                    continue
                incomplete = True
            if not incomplete:
                continue
        symbol = str(record.get("symbol") or "")
        alert_id = f"partial:{record.get('id') or symbol}"
        extra = {
            "position_id": record.get("id"),
            "symbol": symbol,
            "age_seconds": int(age),
        }
        text = f"Partial hedge outstanding for {symbol} ({int(age)}s)"
        alerts.append((alert_id, "partial_hedge_stalled", text, extra))
    return alerts


def _total_notional(records: Iterable[Mapping[str, object]]) -> float:
    total = 0.0
    for record in records:
        status = str(record.get("status") or "").lower()
        if status in {"closed", "simulated"}:
            continue
        if bool(record.get("simulated")):
            continue
        try:
            total += float(record.get("notional_usdt") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def _runaway_alerts(state, now: datetime) -> Iterable[tuple[str, str, str, Dict[str, object]]]:
    alerts: list[tuple[str, str, str, Dict[str, object]]] = []
    records = list_position_records()
    total_limit = _env_float("MAX_TOTAL_NOTIONAL_USDT", 0.0)
    ratio = _env_float("RUNAWAY_NOTIONAL_ALERT_RATIO", 0.8)
    if total_limit > 0 and ratio > 0:
        current = _total_notional(records)
        if current >= total_limit * ratio:
            alert_id = "runaway:notional"
            extra = {
                "current_notional_usdt": float(round(current, 6)),
                "limit_notional_usdt": float(total_limit),
                "ratio": float(ratio),
            }
            text = f"Total notional near limit: {current:.2f} / {total_limit:.2f}"
            alerts.append((alert_id, "runaway_notional_warning", text, extra))

    counters = getattr(state.safety, "counters", None)
    limits = getattr(state.safety, "limits", None)
    if counters and limits:
        current_cancels = getattr(counters, "cancels_last_min", 0)
        cancel_limit = getattr(limits, "max_cancels_per_min", 0)
        cancel_ratio = _env_float("RUNAWAY_CANCEL_ALERT_RATIO", 0.8)
        if cancel_limit > 0 and cancel_ratio > 0:
            if current_cancels >= cancel_limit * cancel_ratio:
                alert_id = "runaway:cancels"
                extra = {
                    "cancels_last_min": int(current_cancels),
                    "cancel_limit": int(cancel_limit),
                    "ratio": float(cancel_ratio),
                }
                text = f"Cancel velocity near limit: {current_cancels}/{cancel_limit}"
                alerts.append((alert_id, "runaway_cancel_rate", text, extra))
    return alerts


def _auto_hedge_alerts(
    now: datetime, *, enabled: bool
) -> Iterable[tuple[str, str, str, Dict[str, object]]]:
    alerts: list[tuple[str, str, str, Dict[str, object]]] = []
    if not enabled:
        return alerts
    auto_state = get_auto_hedge_state()
    heartbeat_threshold = _env_float("AUTO_HEDGE_STALL_ALERT_SECONDS", 180.0)
    success_threshold = _env_float("AUTO_HEDGE_SUCCESS_ALERT_SECONDS", 600.0)
    last_execution_ts = _parse_ts(auto_state.last_execution_ts)
    if heartbeat_threshold > 0:
        stale = False
        if last_execution_ts is None:
            stale = True
        else:
            stale = (now - last_execution_ts).total_seconds() >= heartbeat_threshold
        if stale:
            alert_id = "auto_hedge:stalled"
            extra = {
                "last_execution_ts": auto_state.last_execution_ts,
                "heartbeat_threshold_sec": heartbeat_threshold,
            }
            text = "Auto-hedge daemon heartbeat stalled"
            alerts.append((alert_id, "auto_hedge_stalled", text, extra))

    last_success_ts = _parse_ts(auto_state.last_success_ts)
    if success_threshold > 0:
        success_stale = False
        if last_success_ts is None:
            success_stale = True
        else:
            success_stale = (now - last_success_ts).total_seconds() >= success_threshold
        if success_stale:
            alert_id = "auto_hedge:no_success"
            extra = {
                "last_success_ts": auto_state.last_success_ts,
                "success_threshold_sec": success_threshold,
                "consecutive_failures": auto_state.consecutive_failures,
            }
            text = "Auto-hedge success missing for extended period"
            alerts.append((alert_id, "auto_hedge_no_success", text, extra))
    return alerts


def _hold_unknown_alert(state, now: datetime) -> Iterable[tuple[str, str, str, Dict[str, object]]]:
    alerts: list[tuple[str, str, str, Dict[str, object]]] = []
    safety = getattr(state, "safety", None)
    if not safety or not getattr(safety, "hold_active", False):
        return alerts
    reason = str(getattr(safety, "hold_reason", "") or "").strip().lower()
    if reason and reason not in {"unknown", "na", "n/a"}:
        return alerts
    hold_since_ts = _parse_ts(getattr(safety, "hold_since", None))
    threshold = _env_float("HOLD_UNKNOWN_ALERT_SECONDS", 300.0)
    if threshold <= 0:
        return alerts
    if not hold_since_ts:
        return alerts
    if (now - hold_since_ts).total_seconds() < threshold:
        return alerts
    extra = {
        "hold_since": getattr(safety, "hold_since", None),
        "hold_source": getattr(safety, "hold_source", None),
    }
    text = "HOLD active without known reason"
    alerts.append(("hold:unknown", "hold_unknown_reason", text, extra))
    return alerts


def evaluate_alerts(*, now: datetime | None = None) -> List[Dict[str, object]]:
    """Evaluate risk alerts and emit audit records when anomalies appear."""

    evaluation_ts = now or datetime.now(timezone.utc)
    triggered: MutableMapping[str, bool] = {}

    try:
        state = get_state()
    except Exception as exc:  # pragma: no cover - defensive logging
        LOGGER.warning("risk alerts: unable to acquire runtime state: %s", exc)
        state = None

    if state is not None:
        for alert in _partial_hedge_alerts(
            evaluation_ts,
            threshold=_env_float("PARTIAL_HEDGE_ALERT_SECONDS", 300.0),
        ):
            alert_id, kind, text, extra = alert
            _activate_alert(alert_id=alert_id, kind=kind, text=text, extra=extra, now=evaluation_ts)
            triggered[alert_id] = True

        for alert in _runaway_alerts(state, evaluation_ts):
            alert_id, kind, text, extra = alert
            _activate_alert(alert_id=alert_id, kind=kind, text=text, extra=extra, now=evaluation_ts)
            triggered[alert_id] = True

        auto_enabled = bool(getattr(state.auto_hedge, "enabled", False))
        for alert in _auto_hedge_alerts(evaluation_ts, enabled=auto_enabled):
            alert_id, kind, text, extra = alert
            _activate_alert(alert_id=alert_id, kind=kind, text=text, extra=extra, now=evaluation_ts)
            triggered[alert_id] = True

        for alert in _hold_unknown_alert(state, evaluation_ts):
            alert_id, kind, text, extra = alert
            _activate_alert(alert_id=alert_id, kind=kind, text=text, extra=extra, now=evaluation_ts)
            triggered[alert_id] = True

        risk_guard.evaluate(now=evaluation_ts)

    to_resolve = [alert_id for alert_id in _ACTIVE_ALERTS.keys() if alert_id not in triggered]
    for alert_id in to_resolve:
        _resolve_alert(alert_id, now=evaluation_ts)

    return get_active_alerts()


def get_active_alerts() -> List[Dict[str, object]]:
    return [
        state.as_payload()
        for state in sorted(
            _ACTIVE_ALERTS.values(), key=lambda entry: entry.active_since, reverse=True
        )
    ]


def reset_for_tests() -> None:
    _ACTIVE_ALERTS.clear()


__all__ = ["evaluate_alerts", "get_active_alerts", "reset_for_tests"]
