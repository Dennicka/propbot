from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping


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
    "AlertRecord",
    "AlertsRegistry",
    "REGISTRY",
    "registry",
    "alerts_to_dict",
    "build_alerts_payload",
]
