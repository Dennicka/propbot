"""Helpers for guarding trades against the configured universe."""

from __future__ import annotations

import os
from typing import Set, Tuple

from ..universe_manager import UniverseManager


def _flag_enabled() -> bool:
    raw = os.getenv("ENFORCE_UNIVERSE")
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def is_universe_enforced() -> bool:
    """Return ``True`` when pre-trade universe checks should run."""

    return _flag_enabled()


def _normalise_pair(pair_id: str | None) -> str:
    return str(pair_id or "").strip().upper()


def _current_universe(manager: UniverseManager | None = None) -> Set[str]:
    instance = manager or UniverseManager()
    try:
        pairs = instance.allowed_pairs()
    except AttributeError:
        pairs = set()
    return {entry.upper() for entry in pairs}


def check_pair_allowed(
    pair_id: str | None, *, manager: UniverseManager | None = None
) -> Tuple[bool, str]:
    """Return whether ``pair_id`` is allowed for trading.

    The function returns a ``(ok, reason)`` tuple; ``reason`` is ``"universe"``
    when the pair is not permitted by the current universe snapshot.
    """

    normalised = _normalise_pair(pair_id)
    if not normalised:
        return False, "universe"
    universe_pairs = _current_universe(manager)
    if not universe_pairs:
        return False, "universe"
    if normalised in universe_pairs:
        return True, ""
    return False, "universe"


__all__ = ["check_pair_allowed", "is_universe_enforced"]
