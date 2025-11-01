"""Process-local TTL cache helpers for UI endpoints."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from typing import Tuple, TypeVar

from ..metrics.cache import record_cache_observation

T = TypeVar("T")

__all__ = ["get_or_set", "clear", "reset_for_tests"]


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _monotonic() -> float:
    return time.monotonic()


class _CacheStore:
    def __init__(self, maxsize: int = 256) -> None:
        self._store: "OrderedDict[Hashable, Tuple[float, T]]" = OrderedDict()
        self._lock = threading.Lock()
        self._maxsize = max(1, int(maxsize))

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def get(self, key: Hashable, now: float) -> Tuple[bool, T | None]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return False, None
            expires_at, value = entry
            if expires_at <= now:
                self._store.pop(key, None)
                return False, None
            self._store.move_to_end(key)
            return True, value

    def set(self, key: Hashable, value: T, expires_at: float) -> None:
        with self._lock:
            self._store[key] = (expires_at, value)
            self._store.move_to_end(key)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)


_STORE = _CacheStore()


def clear() -> None:
    """Drop all cached entries."""

    _STORE.clear()


reset_for_tests = clear


async def _ensure_value(loader: Callable[[], T | Awaitable[T]]) -> T:
    value = loader()
    if asyncio.iscoroutine(value) or isinstance(value, Awaitable):
        return await value  # type: ignore[return-value]
    return value  # type: ignore[return-value]


def _default_ttl(ttl: float | None) -> float:
    base = _env_float("UI_CACHE_TTL_DEFAULT_SEC", 2.0)
    if ttl is None or ttl <= 0:
        return max(base, 0.0)
    return ttl


def _cache_enabled() -> bool:
    return _env_flag("UI_CACHE_ENABLED", True)


def _endpoint_label(key: Hashable) -> str:
    if isinstance(key, tuple) and key:
        primary = key[0]
        if isinstance(primary, str):
            return primary
    if isinstance(key, str):
        return key
    return repr(key)


async def get_or_set(
    key: Hashable,
    ttl: float | None,
    loader: Callable[[], T | Awaitable[T]],
) -> T:
    """Return the cached value for ``key`` or compute it using ``loader``."""

    enabled = _cache_enabled()
    now = _monotonic()
    endpoint_label = _endpoint_label(key)

    if enabled:
        hit, cached = _STORE.get(key, now)
        if hit:
            record_cache_observation(endpoint_label, True)
            return cached  # type: ignore[return-value]

    value = await _ensure_value(loader)

    if enabled:
        ttl_seconds = max(_default_ttl(ttl), 0.0)
        expires_at = _monotonic() + ttl_seconds
        _STORE.set(key, value, expires_at)
        record_cache_observation(endpoint_label, False)
    else:
        record_cache_observation(endpoint_label, False)
    return value
