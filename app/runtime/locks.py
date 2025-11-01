"""Async lock registry helpers for idempotent order operations."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Hashable


class _LockRegistry:
    def __init__(self) -> None:
        self._locks: Dict[Hashable, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def _ensure(self, key: Hashable) -> asyncio.Lock:
        async with self._global_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    @asynccontextmanager
    async def acquire(self, *parts: Hashable) -> AsyncIterator[None]:
        key = tuple(parts)
        lock = await self._ensure(key)
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()


_REGISTRY = _LockRegistry()


def order_lock(account: str, venue: str, broker_order_id: str):
    return _REGISTRY.acquire("order", account, venue, broker_order_id)


def symbol_lock(account: str, venue: str, symbol: str, side: str):
    return _REGISTRY.acquire("symbol", account, venue, symbol, side)


def intent_lock(intent_id: str):
    return _REGISTRY.acquire("intent", intent_id)


__all__ = ["order_lock", "symbol_lock", "intent_lock"]

