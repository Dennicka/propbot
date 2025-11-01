"""Testing doubles for broker interactions."""

from __future__ import annotations

from datetime import datetime

from app.broker.base import Broker


class FakeBroker(Broker):
    """Minimal broker implementation used in unit tests."""

    async def create_order(
        self,
        *,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: float | None = None,
        type: str = "LIMIT",
        post_only: bool = True,
        reduce_only: bool = False,
        fee: float = 0.0,
        idemp_key: str | None = None,
    ) -> dict[str, object]:
        return {
            "order_id": 1,
            "venue": venue,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "type": type,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "fee": fee,
            "idemp_key": idemp_key,
        }

    async def cancel(self, *, venue: str, order_id: int) -> None:
        return None

    async def positions(self, *, venue: str) -> dict[str, object]:
        return {"venue": venue, "positions": []}

    async def balances(self, *, venue: str) -> dict[str, object]:
        return {"venue": venue, "balances": []}

    async def get_positions(self) -> list[dict[str, object]]:
        return []

    async def get_fills(self, since: datetime | None = None) -> list[dict[str, object]]:
        return []

    def emit_order_error(self, *_, **__) -> None:
        return None

    def emit_order_latency(self, *_, **__) -> None:
        return None

    def emit_marketdata_staleness(self, *_, **__) -> None:
        return None

    def metrics_tags(self) -> dict[str, str]:
        return {"broker": "fake"}


__all__ = ["FakeBroker"]
