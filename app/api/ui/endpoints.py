"""Compatibility layer for historical UI endpoint imports."""

from __future__ import annotations

from .pretrade import (
    get_pretrade_gate_status,
    get_pretrade_status,
    pretrade_gate_status,
    pretrade_status,
    router,
)

__all__ = [
    "router",
    "pretrade_gate_status",
    "pretrade_status",
    "get_pretrade_gate_status",
    "get_pretrade_status",
]

