from __future__ import annotations

"""Binance USD-M futures websocket resilience helpers."""

from typing import Callable, Iterable, Mapping, Sequence, cast

from app.market.orderbook.book_store import DiffEvent, OrderBookStore
from app.market.streams.base_ws import WsConnector
from app.market.streams.resync import BaseOrderBookStream


def _convert_levels(levels: Sequence[Sequence[float | str]]) -> Iterable[tuple[float, float]]:
    return [(float(price), float(size)) for price, size in levels]


class BinanceOrderBookStream(BaseOrderBookStream):
    def _parse_snapshot(
        self, symbol: str, snapshot: Mapping[str, object]
    ) -> tuple[Iterable[tuple[float, float]], Iterable[tuple[float, float]], int, int | None]:
        last_update = int(snapshot.get("lastUpdateId", 0))
        bids = _convert_levels(snapshot.get("bids", []))
        asks = _convert_levels(snapshot.get("asks", []))
        ts_ms = snapshot.get("ts_ms")
        return bids, asks, last_update, int(ts_ms) if ts_ms is not None else None

    def handle_diff(self, event: DiffEvent) -> None:
        symbol = event["symbol"]
        record = self._orderbook.get_or_create(self.venue, symbol)
        last_seq = record.last_applied_seq
        if last_seq is not None and event["seq_to"] <= last_seq:
            return
        if last_seq is not None and event["seq_from"] <= last_seq:
            adjusted = dict(event)
            adjusted["seq_from"] = last_seq + 1
            super().handle_diff(cast(DiffEvent, adjusted))
            return
        super().handle_diff(event)


def build_binance_stream(
    *,
    orderbook: OrderBookStore,
    connector: WsConnector,
    snapshot_fetcher: Callable[[str], Mapping[str, object]],
) -> BinanceOrderBookStream:
    return BinanceOrderBookStream(
        venue="binance",
        orderbook=orderbook,
        connector=connector,
        snapshot_fetcher=snapshot_fetcher,
    )


__all__ = ["BinanceOrderBookStream", "build_binance_stream"]
