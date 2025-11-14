from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping


OPS_ALERT_SEVERITIES = {"info", "warning", "critical"}


@dataclass(slots=True)
class OpsAlert:
    ts: datetime
    event_type: str
    message: str
    severity: str = "info"
    source: str | None = None
    profile: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)


class OpsAlertsRegistry:
    """Thread-safe in-memory registry of the most recent ops alerts."""

    def __init__(self, maxlen: int = 1000) -> None:
        self._alerts: deque[OpsAlert] = deque(maxlen=maxlen)
        self._lock = threading.RLock()

    def add(self, alert: OpsAlert) -> None:
        if alert.severity not in OPS_ALERT_SEVERITIES:
            raise ValueError(f"invalid severity: {alert.severity!r}")
        with self._lock:
            self._alerts.append(alert)

    def list_recent(
        self,
        limit: int = 100,
        event_type: str | None = None,
        severity: str | None = None,
    ) -> list[OpsAlert]:
        if limit <= 0:
            return []
        if severity is not None and severity not in OPS_ALERT_SEVERITIES:
            return []
        with self._lock:
            snapshot: tuple[OpsAlert, ...] = tuple(self._alerts)
        items: Iterable[OpsAlert] = reversed(snapshot)
        if event_type is not None:
            items = (alert for alert in items if alert.event_type == event_type)
        if severity is not None:
            items = (alert for alert in items if alert.severity == severity)
        result: list[OpsAlert] = []
        for alert in items:
            result.append(alert)
            if len(result) >= limit:
                break
        return result


@dataclass(slots=True)
class AlertRecord:
    """Structured representation of an ops alert."""

    ts: float
    level: str
    source: str
    code: str | None
    message: str
    details: Mapping[str, Any]


class AlertsRegistry:
    """Thread-safe in-memory ring buffer that stores recent alerts."""

    def __init__(self, capacity: int = 200) -> None:
        self._capacity = max(1, int(capacity))
        self._items: list[AlertRecord] = []
        self._pos = 0
        self._lock = threading.RLock()

    def add(self, record: AlertRecord) -> None:
        with self._lock:
            if len(self._items) < self._capacity:
                self._items.append(record)
            else:
                self._items[self._pos] = record
            self._pos = (self._pos + 1) % self._capacity

    def record(
        self,
        level: str,
        source: str,
        message: str,
        *,
        code: str | None = None,
        details: Mapping[str, Any] | None = None,
        ts: float | None = None,
    ) -> AlertRecord:
        timestamp = float(ts if ts is not None else time.time())
        alert = AlertRecord(
            ts=timestamp,
            level=str(level),
            source=str(source),
            code=str(code) if code is not None else None,
            message=str(message),
            details=dict(details) if details else {},
        )
        self.add(alert)
        return alert

    def last(self, limit: int = 50) -> list[AlertRecord]:
        if limit <= 0:
            return []
        with self._lock:
            size = len(self._items)
            if size == 0:
                return []
            count = min(int(limit), size)
            start = (self._pos - count) % size
            ordered: list[AlertRecord] = []
            for index in range(count):
                ordered.append(self._items[(start + index) % size])
        return list(ordered)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._pos = 0


def alerts_to_dict(records: list[AlertRecord]) -> list[dict[str, Any]]:
    """Serialise AlertRecord objects into JSON-friendly dictionaries."""

    payload: list[dict[str, Any]] = []
    for record in records:
        item: dict[str, Any] = {
            "ts": float(record.ts),
            "level": record.level,
            "source": record.source,
            "message": record.message,
        }
        if record.code is not None:
            item["code"] = record.code
        details = dict(record.details) if record.details else {}
        if details:
            item["details"] = details
        payload.append(item)
    return payload


REGISTRY = AlertsRegistry(capacity=500)
# Backwards compatible alias until all imports are updated.
registry = REGISTRY


def build_alerts_payload(limit: int, *, max_limit: int = 200) -> dict[str, Any]:
    safe_limit = max(0, min(int(limit), max_limit))
    records = REGISTRY.last(limit=safe_limit)
    return {"items": alerts_to_dict(records)}


__all__ = [
    "OpsAlert",
    "OpsAlertsRegistry",
    "AlertRecord",
    "AlertsRegistry",
    "REGISTRY",
    "registry",
    "alerts_to_dict",
    "build_alerts_payload",
]
