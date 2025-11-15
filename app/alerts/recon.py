"""Emit structured alerts for reconciliation issues."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Iterable, Sequence

from app.alerts.levels import AlertLevel
from app.alerts.pipeline import RECON_ISSUES_DETECTED, get_ops_alerts_pipeline
from app.recon.models import ReconIssue, ReconSnapshot

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2}
_RECON_ALERT_SOURCE = "recon"


def emit_recon_alerts(snapshot: ReconSnapshot) -> None:
    """Emit alerts for recon issues via the ops alerts pipeline."""

    if not snapshot.issues:
        return

    try:
        _emit_recon_issue_alert(snapshot)
    except Exception:  # pragma: no cover - defensive logging
        logger.exception("recon.emit_alerts_failed", extra={"venue_id": snapshot.venue_id})


def _emit_recon_issue_alert(snapshot: ReconSnapshot) -> None:
    issues: Sequence[ReconIssue] = tuple(snapshot.issues)
    issues_payload = [_serialise_issue(issue) for issue in issues]
    level, ops_severity = _resolve_alert_levels(issues)
    kinds = sorted({issue.kind for issue in issues})
    message = f"Recon issues detected on {snapshot.venue_id}"

    context = {
        "venue_id": snapshot.venue_id,
        "issues": issues_payload,
        "issue_count": len(issues_payload),
        "fingerprint": _build_fingerprint(snapshot.venue_id, issues_payload),
    }
    tags = {
        "venue_id": snapshot.venue_id,
        "issue_count": len(issues_payload),
    }
    if kinds:
        tags["kinds"] = ",".join(kinds)

    pipeline = get_ops_alerts_pipeline()
    pipeline.notify_event(
        event_type=RECON_ISSUES_DETECTED,
        message=message,
        level=level,
        severity=ops_severity,
        context=context,
        tags=tags,
        source=_RECON_ALERT_SOURCE,
    )


def _resolve_alert_levels(issues: Sequence[ReconIssue]) -> tuple[AlertLevel, str]:
    highest = max((_SEVERITY_ORDER.get(issue.severity, 0) for issue in issues), default=0)
    if highest >= _SEVERITY_ORDER["error"]:
        return AlertLevel.ERROR, "critical"
    if highest >= _SEVERITY_ORDER["warning"]:
        return AlertLevel.WARN, "warning"
    return AlertLevel.INFO, "info"


def _serialise_issue(issue: ReconIssue) -> dict[str, str | None]:
    payload: dict[str, str | None] = {
        "severity": issue.severity,
        "kind": issue.kind,
        "message": issue.message,
    }
    if issue.symbol is not None:
        payload["symbol"] = issue.symbol
    if issue.asset is not None:
        payload["asset"] = issue.asset
    if issue.internal_value is not None:
        payload["internal_value"] = str(issue.internal_value)
    if issue.external_value is not None:
        payload["external_value"] = str(issue.external_value)
    return payload


def _build_fingerprint(venue_id: str, issues: Iterable[dict[str, str | None]]) -> str:
    serialised = json.dumps(
        {"venue_id": venue_id, "issues": list(issues)},
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha1(serialised.encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()


__all__ = ["emit_recon_alerts"]
