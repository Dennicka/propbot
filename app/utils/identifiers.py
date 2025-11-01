"""Identifier helpers used for idempotent routing."""

from __future__ import annotations

import os
import secrets
import time


def generate_request_id() -> str:
    """Return a ULID-like sortable identifier."""

    ts = int(time.time() * 1000)
    entropy = secrets.token_hex(10)
    prefix = os.environ.get("REQUEST_ID_PREFIX", "rid")
    return f"{prefix}-{ts:x}-{entropy}"


__all__ = ["generate_request_id"]

