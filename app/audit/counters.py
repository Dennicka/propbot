"""Audit counters for router lifecycle anomalies."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Dict


@dataclass(slots=True)
class AuditCounters:
    """Track counts of lifecycle anomalies detected by the router."""

    duplicate_event: int = 0
    out_of_order: int = 0
    invalid_transition: int = 0
    fill_without_ack: int = 0
    ack_missing_register: int = 0

    def inc(self, name: str) -> None:
        """Increment the named counter, raising ``KeyError`` for invalid names."""

        if not name:
            raise KeyError("counter name must be non-empty")
        if not hasattr(self, name):
            raise KeyError(f"unknown counter: {name}")
        value = getattr(self, name)
        setattr(self, name, int(value) + 1)

    def snapshot(self) -> Dict[str, int]:
        """Return a dictionary snapshot of the counters."""

        return {field.name: int(getattr(self, field.name)) for field in fields(self)}

    def reset(self) -> None:
        """Reset all counters to zero."""

        for field in fields(self):
            setattr(self, field.name, 0)


__all__ = ["AuditCounters"]
