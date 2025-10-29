"""Unified audit timeline combining alerts, approvals, and runtime incidents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Mapping, MutableMapping, Sequence

from ..opsbot import notifier
from ..runtime_state_store import load_runtime_payload
from . import approvals_store


@dataclass(slots=True)
class _AuditEntry:
    timestamp: datetime
    actor: str
    action: str
    status: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        timestamp = self.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        else:
            timestamp = timestamp.astimezone(timezone.utc)
        return {
            "timestamp": timestamp.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "status": self.status,
            "reason": self.reason,
        }


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _clean_reason(*parts: object) -> str:
    values: List[str] = []
    for part in parts:
        if not part:
            continue
        if isinstance(part, Mapping):
            continue
        text = str(part).strip()
        if text:
            values.append(text)
    return " — ".join(values)


def _alert_actor(payload: Mapping[str, object] | None) -> str:
    if not isinstance(payload, Mapping):
        return "system"
    for key in ("actor", "requested_by", "approved_by", "source"):
        value = payload.get(key)
        if value:
            return str(value)
    resume_request = payload.get("resume_request")
    if isinstance(resume_request, Mapping):
        value = resume_request.get("requested_by")
        if value:
            return str(value)
    return "system"


def _alert_reason(kind: str, payload: Mapping[str, object] | None, text: str | None) -> str:
    if not isinstance(payload, Mapping):
        payload = {}
    reason = payload.get("reason")
    if isinstance(reason, Mapping):
        reason = reason.get("reason")
    if reason:
        return str(reason)
    if kind == "risk_limit_requested":
        parts = []
        limit = payload.get("limit")
        value = payload.get("value")
        scope = payload.get("scope")
        if limit:
            if scope:
                parts.append(f"{limit}:{scope}")
            else:
                parts.append(str(limit))
        if value is not None:
            parts.append(f"→ {value}")
        extra_reason = payload.get("reason")
        if extra_reason:
            parts.append(str(extra_reason))
        return " ".join(str(part) for part in parts if part)
    if kind in {"risk_limit_approved", "exit_dry_run_approved"}:
        extra_reason = payload.get("reason")
        if extra_reason:
            return str(extra_reason)
    if text:
        return text
    return ""


def _alert_action(kind: str) -> tuple[str, str]:
    mapping = {
        "safety_hold": ("Safety hold engaged", "applied"),
        "risk_guard_force_hold": ("Auto-throttle HOLD", "applied"),
        "resume_requested": ("Resume requested", "pending"),
        "resume_confirmed": ("Resume approved", "approved"),
        "risk_limit_requested": ("Risk limit raise", "pending"),
        "risk_limit_approved": ("Risk limit raise", "approved"),
        "exit_dry_run_requested": ("Exit DRY_RUN_MODE", "pending"),
        "exit_dry_run_approved": ("Exit DRY_RUN_MODE", "approved"),
        "kill_switch": ("Kill switch", "applied"),
        "mode_change": ("Mode change", "applied"),
        "cancel_all": ("Cancel-all", "applied"),
        "flatten_requested": ("Flatten exposure", "pending"),
        "manual_hedge_execute": ("Manual hedge execute", "applied"),
        "manual_hedge_confirm": ("Manual hedge confirm", "approved"),
        "auto_hedge_failure": ("Auto hedge failure", "applied"),
        "auto_hedge_executed": ("Auto hedge executed", "applied"),
        "watchdog_alert": ("Watchdog alert", "applied"),
    }
    default_action = kind.replace("_", " ").title() if kind else "Event"
    default_status = "applied"
    return mapping.get(kind, (default_action, default_status))


def _from_alerts(alerts: Sequence[Mapping[str, object]]) -> List[_AuditEntry]:
    entries: List[_AuditEntry] = []
    for alert in alerts:
        if not isinstance(alert, Mapping):
            continue
        timestamp = _parse_timestamp(alert.get("ts")) or datetime.now(timezone.utc)
        kind = str(alert.get("kind") or "")
        text = str(alert.get("text") or "")
        extra = alert.get("extra") if isinstance(alert.get("extra"), Mapping) else None
        actor = _alert_actor(extra)
        action, status = _alert_action(kind)
        reason = _alert_reason(kind, extra, text)
        entries.append(
            _AuditEntry(
                timestamp=timestamp,
                actor=actor or "system",
                action=action,
                status=status,
                reason=reason,
            )
        )
    return entries


def _format_parameters(parameters: Mapping[str, object] | None) -> str:
    if not isinstance(parameters, Mapping):
        return ""
    reason = parameters.get("reason")
    limit = parameters.get("limit")
    scope = parameters.get("scope")
    value = parameters.get("value")
    parts: List[str] = []
    if reason:
        parts.append(str(reason))
    detail_parts: List[str] = []
    if limit:
        detail = str(limit)
        if scope not in (None, ""):
            detail += f" ({scope})"
        detail_parts.append(detail)
    if value not in (None, ""):
        detail_parts.append(f"→ {value}")
    if detail_parts:
        parts.append(" ".join(detail_parts))
    return " — ".join(parts)


def _from_approvals(records: Sequence[Mapping[str, object]]) -> List[_AuditEntry]:
    entries: List[_AuditEntry] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        status = str(record.get("status") or "pending").lower()
        if status not in {"pending", "approved"}:
            continue
        if status == "pending":
            timestamp = _parse_timestamp(record.get("requested_ts"))
            actor = record.get("requested_by")
        else:
            timestamp = _parse_timestamp(record.get("approved_ts")) or _parse_timestamp(
                record.get("requested_ts")
            )
            actor = record.get("approved_by") or record.get("requested_by")
        if timestamp is None:
            timestamp = datetime.now(timezone.utc)
        action = str(record.get("action") or "approval").replace("_", " ").title()
        reason = _format_parameters(record.get("parameters"))
        entries.append(
            _AuditEntry(
                timestamp=timestamp,
                actor=str(actor or "system"),
                action=action,
                status=status,
                reason=reason,
            )
        )
    return entries


def _from_incidents(payload: Mapping[str, object]) -> List[_AuditEntry]:
    incidents = payload.get("incidents")
    if not isinstance(incidents, Sequence):
        return []
    entries: List[_AuditEntry] = []
    for incident in incidents:
        if not isinstance(incident, Mapping):
            continue
        timestamp = _parse_timestamp(incident.get("ts"))
        if timestamp is None:
            continue
        details = incident.get("details")
        actor = "system"
        reason = ""
        if isinstance(details, Mapping):
            actor = str(details.get("source") or details.get("actor") or "system")
            reason = _clean_reason(details.get("reason"), details.get("detail"))
        action = str(incident.get("kind") or "incident").replace("_", " ").title()
        entries.append(
            _AuditEntry(
                timestamp=timestamp,
                actor=actor,
                action=action,
                status="applied",
                reason=reason,
            )
        )
    return entries


def list_recent_events(limit: int = 100) -> List[dict[str, str]]:
    """Return a merged, chronologically sorted incident timeline."""

    limit = max(1, min(int(limit), 500))
    alerts = notifier.read_audit_events(limit=limit * 3)
    approvals = approvals_store.list_requests()
    payload = load_runtime_payload()
    runtime_payload = payload if isinstance(payload, MutableMapping) else {}

    combined: List[_AuditEntry] = []
    combined.extend(_from_alerts(alerts))
    combined.extend(_from_approvals(approvals))
    combined.extend(_from_incidents(runtime_payload))

    combined.sort(key=lambda entry: entry.timestamp, reverse=True)
    sliced = combined[:limit]
    return [entry.as_dict() for entry in sliced]


__all__ = ["list_recent_events"]
