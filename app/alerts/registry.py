from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Mapping, Sequence


class AlertsRegistry:
    """In-memory ring buffer of recent alerts for ops visibility."""

    def __init__(self, limit: int = 100) -> None:
        self._limit = max(1, int(limit))
        self._lock = threading.RLock()
        self._buffer: Deque[dict[str, Any]] = deque(maxlen=self._limit)

    def record(
        self,
        *,
        level: str,
        message: str,
        meta: Mapping[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": float(ts if ts is not None else time.time()),
            "level": str(level),
            "message": str(message),
        }
        if meta:
            entry["meta"] = dict(meta)
        with self._lock:
            self._buffer.append(entry)

    def last(self, limit: int | None = None) -> Sequence[Mapping[str, Any]]:
        count = int(limit) if limit is not None else self._limit
        if count <= 0:
            return []
        with self._lock:
            items = list(self._buffer)[-count:]
        return [dict(item) for item in items[::-1]]

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


registry = AlertsRegistry()


__all__ = ["AlertsRegistry", "registry"]
