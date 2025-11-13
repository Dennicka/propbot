"""Cooldown registry for router antiflood protection."""

from __future__ import annotations

import time
from typing import Dict


def _coerce_ttl(value: int | float | None, default: float) -> float:
    if value is None:
        return float(default)
    try:
        ttl = float(value)
    except (TypeError, ValueError):
        return float(default)
    return float(ttl)


class CooldownRegistry:
    """Track cooldown deadlines for router keys."""

    def __init__(self, default_ttl: int = 5, max_items: int = 100_000) -> None:
        self._default_ttl = max(float(default_ttl), 0.0)
        self._max_items = max(int(max_items), 0)
        self._deadlines: Dict[str, float] = {}
        self._reasons: Dict[str, str] = {}

    @property
    def default_ttl(self) -> float:
        return self._default_ttl

    def hit(
        self,
        key: str,
        seconds: int | float | None = None,
        *,
        reason: str = "",
        now: float | None = None,
    ) -> None:
        reference = self._resolve_now(now)
        ttl_seconds = max(_coerce_ttl(seconds, self._default_ttl), 0.0)
        self.cleanup(reference)
        if ttl_seconds <= 0:
            self._deadlines.pop(key, None)
            self._reasons.pop(key, None)
            return
        deadline = reference + ttl_seconds
        self._deadlines[key] = deadline
        if reason:
            self._reasons[key] = reason
        else:
            self._reasons.pop(key, None)
        self._enforce_capacity()

    def remaining(self, key: str, *, now: float | None = None) -> float:
        reference = self._resolve_now(now)
        self.cleanup(reference)
        deadline = self._deadlines.get(key)
        if deadline is None:
            return 0.0
        remaining = deadline - reference
        if remaining <= 0:
            self._deadlines.pop(key, None)
            self._reasons.pop(key, None)
            return 0.0
        return remaining

    def is_cooling(self, key: str, *, now: float | None = None) -> bool:
        return self.remaining(key, now=now) > 0.0

    def cleanup(self, now: float | None = None) -> None:
        if not self._deadlines:
            return
        reference = self._resolve_now(now)
        expired = [key for key, deadline in self._deadlines.items() if deadline <= reference]
        if expired:
            for key in expired:
                self._deadlines.pop(key, None)
                self._reasons.pop(key, None)

    def last_reason(self, key: str) -> str:
        return self._reasons.get(key, "")

    def _resolve_now(self, now: float | None) -> float:
        if now is None:
            return float(time.time())
        return float(now)

    def _enforce_capacity(self) -> None:
        if self._max_items <= 0:
            return
        if len(self._deadlines) <= self._max_items:
            return
        for key in sorted(self._deadlines, key=self._deadlines.__getitem__):
            if len(self._deadlines) <= self._max_items:
                break
            self._deadlines.pop(key, None)
            self._reasons.pop(key, None)


__all__ = ["CooldownRegistry"]
