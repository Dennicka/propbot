from __future__ import annotations

from typing import Iterable

from . import InMemoryDerivClient, build_in_memory_client


def create_client(symbols: Iterable[str], safe_mode: bool = True) -> InMemoryDerivClient:
    return build_in_memory_client("okx_perp", symbols)
