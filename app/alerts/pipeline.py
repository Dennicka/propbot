from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone
import threading
from typing import Mapping

from .levels import AlertLevel
from .manager import notify
from .registry import OpsAlert, OpsAlertsRegistry, OPS_ALERT_SEVERITIES

RISK_LIMIT_BREACHED = "risk_limit_breached"
PNL_CAP_BREACHED = "pnl_cap_breached"


def _normalise_mapping(payload: Mapping[str, object]) -> dict[str, object]:
    normalised: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, Mapping):
            normalised[key] = _normalise_mapping(value)
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            normalised[key] = value
            continue
        if isinstance(value, Decimal):
            normalised[key] = format(value, "f")
            continue
        normalised[key] = str(value)
    return normalised


class OpsAlertsPipeline:
    """Dispatch structured ops alerts to the notifier stack."""

    def __init__(
        self,
        *,
        default_level: AlertLevel | str = AlertLevel.INFO,
        default_source: str = "ops",
        registry: OpsAlertsRegistry | None = None,
    ) -> None:
        self._default_level = AlertLevel.coerce(default_level)
        self._default_source = default_source
        self._registry = registry

    def notify_event(
        self,
        *,
        event_type: str,
        message: str,
        level: AlertLevel | str | None = None,
        severity: str | None = None,
        context: Mapping[str, object] | None = None,
        tags: Mapping[str, object] | None = None,
        code: str | None = None,
        source: str | None = None,
        profile: str | None = None,
    ) -> None:
        resolved_level = AlertLevel.coerce(level or self._default_level)
        payload: dict[str, object] = {"event_type": event_type}
        if context:
            payload["context"] = _normalise_mapping(context)
        if tags:
            payload["tags"] = _normalise_mapping(tags)
        resolved_source = source or self._default_source
        notify(resolved_level, message, source=resolved_source, code=code, **payload)

        if self._registry is not None:
            resolved_context = _normalise_mapping(context) if context else {}
            registry_severity = self._resolve_severity(severity, resolved_level)
            alert = OpsAlert(
                ts=datetime.now(timezone.utc),
                event_type=event_type,
                message=message,
                severity=registry_severity,
                source=resolved_source,
                profile=profile,
                context=resolved_context,
            )
            self._registry.add(alert)

    def notify_text(
        self,
        message: str,
        *,
        event_type: str = "text",
        level: AlertLevel | str | None = None,
        severity: str | None = None,
        context: Mapping[str, object] | None = None,
        tags: Mapping[str, object] | None = None,
        code: str | None = None,
        source: str | None = None,
        profile: str | None = None,
    ) -> None:
        self.notify_event(
            event_type=event_type,
            message=message,
            level=level,
            severity=severity,
            context=context,
            tags=tags,
            code=code,
            source=source,
            profile=profile,
        )

    def notify_exception(
        self,
        message: str,
        *,
        event_type: str = "exception",
        level: AlertLevel | str | None = AlertLevel.ERROR,
        severity: str | None = None,
        context: Mapping[str, object] | None = None,
        tags: Mapping[str, object] | None = None,
        code: str | None = None,
        source: str | None = None,
        profile: str | None = None,
    ) -> None:
        self.notify_event(
            event_type=event_type,
            message=message,
            level=level,
            severity=severity,
            context=context,
            tags=tags,
            code=code,
            source=source,
            profile=profile,
        )

    def attach_registry(self, registry: OpsAlertsRegistry) -> None:
        self._registry = registry

    @staticmethod
    def _resolve_severity(
        severity: str | None,
        level: AlertLevel,
    ) -> str:
        if severity:
            normalised = severity.strip().lower()
            if normalised in OPS_ALERT_SEVERITIES:
                return normalised
        if level is AlertLevel.CRITICAL:
            return "critical"
        if level in {AlertLevel.WARN, AlertLevel.ERROR}:
            return "warning"
        return "info"


_PIPELINE: OpsAlertsPipeline | None = None
_PIPELINE_LOCK = threading.Lock()


def get_ops_alerts_pipeline(*, registry: OpsAlertsRegistry | None = None) -> OpsAlertsPipeline:
    global _PIPELINE
    if _PIPELINE is None:
        with _PIPELINE_LOCK:
            if _PIPELINE is None:
                _PIPELINE = OpsAlertsPipeline(registry=registry)
    elif registry is not None:
        _PIPELINE.attach_registry(registry)
    return _PIPELINE


__all__ = [
    "OpsAlertsPipeline",
    "PNL_CAP_BREACHED",
    "RISK_LIMIT_BREACHED",
    "get_ops_alerts_pipeline",
]
