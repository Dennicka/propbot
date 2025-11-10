from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence


@dataclass(frozen=True)
class CancelAllResult:
    """Typed response for idempotent cancel-all operations."""

    ok: bool
    cleared: int = 0
    failed: int = 0
    order_ids: Sequence[int] = ()
    details: Optional[Dict[str, object]] = None

    def as_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "ok": bool(self.ok),
            "cleared": int(self.cleared),
            "failed": int(self.failed),
        }
        if self.order_ids:
            payload["order_ids"] = [int(order_id) for order_id in self.order_ids]
        if self.details:
            payload["details"] = dict(self.details)
        return payload


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
        tif: str | None = None,
        strategy: str | None = None,
        post_only: bool = True,
        reduce_only: bool = False,
        fee: float = 0.0,
        idemp_key: str | None = None,
    ) -> Dict[str, object]:
        """Submit an order to the broker and return a serialisable payload."""

        ...

    @abstractmethod
    async def cancel(self, *, venue: str, order_id: int) -> None:
        """Cancel a single order on the broker."""

        ...

    @abstractmethod
    async def positions(self, *, venue: str) -> Dict[str, object]:
        """Return the current positions for ``venue`` in broker-native format."""

        ...

    @abstractmethod
    async def balances(self, *, venue: str) -> Dict[str, object]:
        """Return wallet balances for ``venue`` in broker-native format."""

        ...

    @abstractmethod
    async def get_positions(self) -> List[Dict[str, object]]:
        """Return normalised exposure snapshot for the broker venue."""

    @abstractmethod
    async def get_fills(self, since: datetime | None = None) -> List[Dict[str, object]]:
        """Return fills executed on the broker since the optional timestamp."""

    # ------------------------------------------------------------------
    # Optional reconciliation helpers (safe no-ops by default)
    # ------------------------------------------------------------------
    async def get_order_by_client_id(self, client_id: str) -> Dict[str, object] | None:
        return None

    async def get_recently_closed_symbols(
        self, *, since: datetime | None = None
    ) -> List[str]:  # pragma: no cover - default hook
        return []

    async def positions_snapshot(
        self, *, venue: str | None = None
    ) -> List[Dict[str, object]]:  # pragma: no cover - default hook
        return []

    async def cancel_all_orders_idempotent(
        self,
        *,
        venue: str | None = None,
        correlation_id: str | None = None,
        orders: Iterable[Dict[str, object]] | None = None,
    ) -> CancelAllResult:  # pragma: no cover - default hook
        return CancelAllResult(ok=True, cleared=0, failed=0)

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
