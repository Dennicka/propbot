"""Router adapter helpers for deterministic identifiers."""

from __future__ import annotations

import hashlib
import time
from typing import Any

_DEFAULT_BUCKET_SECONDS = 60


def _normalise_component(value: Any) -> str:
    text = str(value or "").strip()
    return text.lower()


def _normalise_symbol(value: Any) -> str:
    text = str(value or "").strip()
    return text.upper()


def _bucket(ts: float | None, bucket_seconds: int) -> int:
    timestamp = float(ts if ts is not None else time.time())
    if bucket_seconds <= 0:
        bucket_seconds = _DEFAULT_BUCKET_SECONDS
    return int(timestamp // bucket_seconds)


def generate_client_order_id(
    strategy: str | None,
    venue: str | None,
    symbol: str | None,
    side: str | None,
    *,
    timestamp: float | None = None,
    nonce: str | None = None,
    bucket_seconds: int = _DEFAULT_BUCKET_SECONDS,
) -> str:
    """Return a deterministic client order identifier.

    ``strategy``, ``venue``, ``symbol`` and ``side`` are normalised before being
    combined with a timestamp bucket and ``nonce``. The helper is idempotent:
    passing the same inputs yields the same identifier, which allows retries to
    reuse the original ``clientOrderId``.
    """

    strategy_key = _normalise_component(strategy)
    venue_key = _normalise_component(venue)
    symbol_key = _normalise_symbol(symbol)
    side_key = _normalise_component(side)
    bucket = _bucket(timestamp, bucket_seconds)
    nonce_key = str(nonce or f"{strategy_key}:{symbol_key}:{side_key}:{bucket}")
    payload = "|".join([strategy_key, venue_key, symbol_key, side_key, str(bucket), nonce_key])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"cid-{bucket:x}-{digest[:24]}"


__all__ = ["generate_client_order_id"]
