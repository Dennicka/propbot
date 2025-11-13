"""Client order id generation and in-memory idempotency tracking."""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any, Mapping

from .state import OrderState

LOGGER = logging.getLogger(__name__)

stats: dict[str, int] = {
    "touch": 0,
    "dupe": 0,
    "removed_ttl": 0,
    "removed_size": 0,
}


class IntentWindow:
    """Track recent intents to avoid submitting duplicates within a window."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 3,
        max_items: int = 100_000,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max_items = int(max_items)
        self._entries: dict[str, float] = {}

    def is_duplicate(self, key: str, now: float | None = None) -> bool:
        reference = float(now) if now is not None else time.time()
        ts = self._entries.get(key)
        if ts is None:
            return False
        if self._ttl <= 0:
            return False
        if self._ttl > 0 and reference - ts > self._ttl:
            self._entries.pop(key, None)
            return False
        stats["dupe"] += 1
        return True

    def touch(self, key: str, now: float | None = None) -> None:
        reference = float(now) if now is not None else time.time()
        self._entries[key] = reference
        stats["touch"] += 1
        self._enforce_capacity()

    def cleanup(self, now: float | None = None) -> tuple[int, int]:
        reference = float(now) if now is not None else time.time()
        removed_ttl = 0
        if self._ttl > 0:
            cutoff = reference - self._ttl
            expired = [key for key, ts in self._entries.items() if ts <= cutoff]
            for key in expired:
                self._entries.pop(key, None)
            removed_ttl = len(expired)
        removed_size = 0
        if self._max_items > 0 and len(self._entries) > self._max_items:
            excess = len(self._entries) - self._max_items
            oldest = sorted(self._entries.items(), key=lambda item: item[1])[:excess]
            for key, _ in oldest:
                self._entries.pop(key, None)
            removed_size = len(oldest)
        if removed_ttl:
            stats["removed_ttl"] += removed_ttl
        if removed_size:
            stats["removed_size"] += removed_size
        return removed_ttl, removed_size

    def _enforce_capacity(self) -> None:
        if self._max_items <= 0 or len(self._entries) <= self._max_items:
            return
        excess = len(self._entries) - self._max_items
        oldest = sorted(self._entries.items(), key=lambda item: item[1])[:excess]
        for key, _ in oldest:
            self._entries.pop(key, None)
        removed = len(oldest)
        if removed:
            stats["removed_size"] += removed

    def forget(self, key: str) -> None:
        self._entries.pop(key, None)


_PREFIX = "PB"
_DEFAULT_TTL = 24 * 60 * 60  # 24 hours
_DEFAULT_MAX_ENTRIES = 2048
_DECIMAL_QUANT = Decimal("1e-8")


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


def generate_key(intent: Mapping[str, Any]) -> str:
    """Return a stable identifier for an order intent."""

    def _normalise_string(value: Any, *, lower: bool = False) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text.lower() if lower else text

    def _normalise_decimal(value: Any) -> str | None:
        if value is None:
            return None
        try:
            dec_value = Decimal(str(value)).quantize(_DECIMAL_QUANT, rounding=ROUND_HALF_EVEN)
        except (InvalidOperation, ValueError) as exc:  # pragma: no cover - defensive
            raise ValueError(f"invalid numeric value: {value!r}") from exc
        return format(dec_value, ".8f")

    fields = (
        _normalise_string(intent.get("venue")),
        _normalise_string(intent.get("symbol")),
        _normalise_string(intent.get("side"), lower=True),
        _normalise_decimal(intent.get("price")),
        _normalise_decimal(intent.get("qty")),
        _normalise_string(intent.get("strategy"), lower=True),
        _normalise_string(intent.get("client_tag")),
        _normalise_string(intent.get("parent_id")),
    )
    payload = repr(fields).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "IdempoStore",
    "IntentWindow",
    "generate_key",
    "make_coid",
    "stats",
]
