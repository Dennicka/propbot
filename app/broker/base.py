from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional


class Broker(ABC):
    """Abstract broker interface used by the execution router."""

    @abstractmethod
    async def create_order(
        self,
        *,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float] = None,
        type: str = "LIMIT",
        post_only: bool = True,
        reduce_only: bool = False,
        fee: float = 0.0,
        idemp_key: str | None = None,
    ) -> Dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, *, venue: str, order_id: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def positions(self, *, venue: str) -> Dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    async def balances(self, *, venue: str) -> Dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    async def get_positions(self) -> List[Dict[str, object]]:
        """Return normalised exposure snapshot for the broker venue."""

    @abstractmethod
    async def get_fills(self, since: datetime | None = None) -> List[Dict[str, object]]:
        """Return fills executed on the broker since the optional timestamp."""

    # ------------------------------------------------------------------
    # Optional telemetry hooks (safe no-ops by default)
    # ------------------------------------------------------------------
    def emit_order_error(self, *_, **__) -> None:  # pragma: no cover - default hook
        return None

    def emit_order_latency(self, *_, **__) -> None:  # pragma: no cover - default hook
        return None

    def emit_marketdata_staleness(self, *_, **__) -> None:  # pragma: no cover - default hook
        return None

    def metrics_tags(self) -> dict[str, str]:  # pragma: no cover - default hook
        return {"broker": getattr(self, "name", "unknown")}
