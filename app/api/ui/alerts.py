from __future__ import annotations

from fastapi import APIRouter, Query

from ...services import runtime

router = APIRouter(prefix="/api/ui", tags=["ui"])


@router.get("/alerts")
def get_alerts(
    limit: int = Query(100, ge=1, le=500),
    event_type: str | None = None,
    severity: str | None = Query(None, pattern="^(info|warning|critical)$"),
) -> list[dict]:
    registry = runtime.get_ops_alerts_registry()
    alerts = registry.list_recent(limit=limit, event_type=event_type, severity=severity)
    return [
        {
            "ts": alert.ts.isoformat(),
            "event_type": alert.event_type,
            "message": alert.message,
            "severity": alert.severity,
            "source": alert.source,
            "profile": alert.profile,
            "context": dict(alert.context),
        }
        for alert in alerts
    ]
