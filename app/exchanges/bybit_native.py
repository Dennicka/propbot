from __future__ import annotations

"""Bybit perpetual futures websocket resilience helpers."""

from typing import Callable, Iterable, Mapping, Sequence

from app.market.orderbook.book_store import DiffEvent, OrderBookStore
from app.market.streams.base_ws import WsConnector
from app.market.streams.resync import BaseOrderBookStream


def _convert(levels: Sequence[Sequence[float | str]]) -> Iterable[tuple[float, float]]:
    return [(float(price), float(size)) for price, size in levels]


class BybitOrderBookStream(BaseOrderBookStream):
    def _parse_snapshot(
        self, symbol: str, snapshot: Mapping[str, object]
    ) -> tuple[Iterable[tuple[float, float]], Iterable[tuple[float, float]], int, int | None]:
        seq = int(snapshot.get("seq", 0))
        bids = _convert(snapshot.get("bids", []))
        asks = _convert(snapshot.get("asks", []))
        ts_ms = snapshot.get("ts_ms")
        return bids, asks, seq, int(ts_ms) if ts_ms is not None else None

    def _validate_diff(self, symbol: str, event: DiffEvent) -> bool:
        record = self._orderbook.get_or_create(self.venue, symbol)
        last_seq = record.last_applied_seq
        if last_seq is None:
            return True
        expected = last_seq + 1
        return event["seq_from"] == expected


def build_bybit_stream(
    *,
    orderbook: OrderBookStore,
    connector: WsConnector,
    snapshot_fetcher: Callable[[str], Mapping[str, object]],
) -> BybitOrderBookStream:
    return BybitOrderBookStream(
        venue="bybit",
        orderbook=orderbook,
        connector=connector,
        snapshot_fetcher=snapshot_fetcher,
    )


__all__ = ["BybitOrderBookStream", "build_bybit_stream"]
