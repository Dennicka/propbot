from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import threading
from typing import Mapping

from .levels import AlertLevel
from .manager import notify

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


@dataclass(slots=True)
class OpsAlert:
    event_type: str
    message: str
    level: AlertLevel
    context: Mapping[str, object] | None = None
    tags: Mapping[str, object] | None = None
    code: str | None = None
    source: str | None = None


class OpsAlertsPipeline:
    """Dispatch structured ops alerts to the notifier stack."""

    def __init__(
        self,
        *,
        default_level: AlertLevel | str = AlertLevel.INFO,
        default_source: str = "ops",
    ) -> None:
        self._default_level = AlertLevel.coerce(default_level)
        self._default_source = default_source

    def notify_event(
        self,
        *,
        event_type: str,
        message: str,
        level: AlertLevel | str | None = None,
        context: Mapping[str, object] | None = None,
        tags: Mapping[str, object] | None = None,
        code: str | None = None,
        source: str | None = None,
    ) -> None:
        payload: dict[str, object] = {"event_type": event_type}
        if context:
            payload["context"] = _normalise_mapping(context)
        if tags:
            payload["tags"] = _normalise_mapping(tags)
        notify(
            level or self._default_level,
            message,
            source=source or self._default_source,
            code=code,
            **payload,
        )

    def notify_text(
        self,
        message: str,
        *,
        event_type: str = "text",
        level: AlertLevel | str | None = None,
        context: Mapping[str, object] | None = None,
        tags: Mapping[str, object] | None = None,
        code: str | None = None,
        source: str | None = None,
    ) -> None:
        self.notify_event(
            event_type=event_type,
            message=message,
            level=level,
            context=context,
            tags=tags,
            code=code,
            source=source,
        )

    def notify_exception(
        self,
        message: str,
        *,
        event_type: str = "exception",
        level: AlertLevel | str | None = AlertLevel.ERROR,
        context: Mapping[str, object] | None = None,
        tags: Mapping[str, object] | None = None,
        code: str | None = None,
        source: str | None = None,
    ) -> None:
        self.notify_event(
            event_type=event_type,
            message=message,
            level=level,
            context=context,
            tags=tags,
            code=code,
            source=source,
        )


_PIPELINE: OpsAlertsPipeline | None = None
_PIPELINE_LOCK = threading.Lock()


def get_ops_alerts_pipeline() -> OpsAlertsPipeline:
    global _PIPELINE
    if _PIPELINE is None:
        with _PIPELINE_LOCK:
            if _PIPELINE is None:
                _PIPELINE = OpsAlertsPipeline()
    return _PIPELINE


__all__ = [
    "OpsAlert",
    "OpsAlertsPipeline",
    "PNL_CAP_BREACHED",
    "RISK_LIMIT_BREACHED",
    "get_ops_alerts_pipeline",
]
