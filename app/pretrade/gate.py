"""Lightweight runtime gate for pre-trade throttling decisions."""

from __future__ import annotations

import time
from typing import Any, Mapping, Tuple


class PreTradeGate:
    """Tracks whether order submission should be throttled before risk checks."""

    __slots__ = ("is_throttled", "reason", "last_updated_ts")

    def __init__(self) -> None:
        self.is_throttled: bool = False
        self.reason: str | None = None
        self.last_updated_ts: float = 0.0

    # ------------------------------------------------------------------
    def set_throttled(self, reason: str) -> bool:
        """Activate throttling with the supplied reason.

        Returns ``True`` when the state changed (useful to avoid duplicate
        logging) and ``False`` otherwise.
        """

        reason_text = (reason or "THROTTLED").strip() or "THROTTLED"
        changed = (not self.is_throttled) or (self.reason != reason_text)
        self.is_throttled = True
        self.reason = reason_text
        if changed:
            self.last_updated_ts = time.time()
        return changed

    # ------------------------------------------------------------------
    def clear(self) -> bool:
        """Clear the throttle flag.

        Returns ``True`` when the state changed.
        """

        if not self.is_throttled and self.reason is None:
            return False
        self.is_throttled = False
        self.reason = None
        self.last_updated_ts = time.time()
        return True

    # ------------------------------------------------------------------
    def check_allowed(self, order_ctx: Mapping[str, Any] | None) -> Tuple[bool, str | None]:
        """Return whether the current request is allowed to proceed."""

        if self.is_throttled:
            return False, self.reason or "THROTTLED"
        return True, None

    # ------------------------------------------------------------------
    def is_throttled_by(self, reason: str) -> bool:
        """Return ``True`` when the gate is throttled for the supplied reason."""

        if not self.is_throttled:
            return False
        if reason is None:
            return False
        try:
            expected = (str(reason) or "").strip()
        except Exception:  # pragma: no cover - defensive coercion
            return False
        if not expected:
            return False
        current = (self.reason or "").strip()
        return current == expected

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, object | None]:
        """Expose the gate state for UI/runtime snapshots."""

        return {
            "throttled": bool(self.is_throttled),
            "reason": self.reason,
            "updated_ts": self.last_updated_ts if self.last_updated_ts else None,
        }


__all__ = ["PreTradeGate"]
