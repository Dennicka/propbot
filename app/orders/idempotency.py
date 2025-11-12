"""Client order id generation and in-memory idempotency tracking."""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from .state import OrderState

LOGGER = logging.getLogger(__name__)

_PREFIX = "PB"
_DEFAULT_TTL = 24 * 60 * 60  # 24 hours
_DEFAULT_MAX_ENTRIES = 2048


@dataclass(slots=True)
class _Entry:
    state: OrderState
    filled_qty: float
    last_update: float


def make_coid(
    strategy: str,
    venue: str,
    symbol: str,
    side: str,
    ts_ns: int,
    nonce: int,
) -> str:
    """Derive a deterministic client order identifier."""

    payload = "|".join(
        (
            str(strategy or "").strip().lower(),
            str(venue or "").strip().lower(),
            str(symbol or "").strip().lower(),
            str(side or "").strip().lower(),
            str(int(ts_ns)),
            str(int(nonce)),
        )
    ).encode("utf-8")
    digest = hashlib.blake2b(payload, digest_size=10).digest()
    token = base64.b32encode(digest).decode("ascii").rstrip("=")
    client_order_id = f"{_PREFIX}{token}"[:32]
    return client_order_id


class IdempoStore:
    """Simple LRU+TTL cache for client order identifiers."""

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()

    def should_send(self, coid: str) -> bool:
        """Return True if the order should be transmitted."""

        now = time.time()
        self._purge_expired(now)
        entry = self._entries.get(coid)
        if entry is not None:
            entry.last_update = now
            self._entries.move_to_end(coid)
            LOGGER.debug(
                "order_idempotency.duplicate_detected",
                extra={
                    "event": "order_idempotency_duplicate_detected",
                    "component": "orders_idempotency",
                    "details": {"client_order_id": coid, "state": entry.state.value},
                },
            )
            return False
        self._entries[coid] = _Entry(OrderState.NEW, 0.0, now)
        self._ensure_capacity()
        return True

    def mark_ack(self, coid: str) -> None:
        self._touch(coid, OrderState.ACK)

    def mark_fill(self, coid: str, qty: float) -> None:
        entry = self._touch(coid, OrderState.PARTIAL)
        increment = float(qty)
        if increment < 0:
            increment = 0.0
        entry.filled_qty = max(entry.filled_qty, increment)
        entry.last_update = time.time()

    def mark_cancel(self, coid: str) -> None:
        entry = self._touch(coid, OrderState.CANCELED)
        entry.last_update = time.time()

    def expire(self, coid: str) -> None:
        removed = self._entries.pop(coid, None)
        if removed is not None:
            LOGGER.debug(
                "order_idempotency.expired",
                extra={
                    "event": "order_idempotency_expired",
                    "component": "orders_idempotency",
                    "details": {"client_order_id": coid},
                },
            )

    def _touch(self, coid: str, state: OrderState) -> _Entry:
        now = time.time()
        entry = self._entries.get(coid)
        if entry is None:
            LOGGER.warning(
                "order_idempotency.implicit_registration",
                extra={
                    "event": "order_idempotency_implicit_registration",
                    "component": "orders_idempotency",
                    "details": {"client_order_id": coid, "state": state.value},
                },
            )
            entry = _Entry(state, 0.0, now)
            self._entries[coid] = entry
            self._ensure_capacity()
        else:
            entry.state = state
            entry.last_update = now
            self._entries.move_to_end(coid)
        return entry

    def _purge_expired(self, now: float) -> None:
        if self._ttl <= 0:
            return
        keys_to_delete: list[str] = []
        for key, entry in self._entries.items():
            if now - entry.last_update > self._ttl:
                keys_to_delete.append(key)
        for key in keys_to_delete:
            self._entries.pop(key, None)

    def _ensure_capacity(self) -> None:
        while self._max_entries > 0 and len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


__all__ = ["IdempoStore", "make_coid"]
