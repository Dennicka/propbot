from __future__ import annotations

from typing import Iterable

from . import InMemoryDerivClient, build_in_memory_client


def create_client(symbols: Iterable[str], safe_mode: bool = True) -> InMemoryDerivClient:
    """Return a deterministic client for paper/test environments."""
    # For SAFE_MODE we rely on deterministic in-memory implementation.
    # Future work: plug real REST/WS clients when SAFE_MODE is disabled.
    return build_in_memory_client("binance_um", symbols)
