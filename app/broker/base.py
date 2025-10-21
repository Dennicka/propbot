from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional


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
