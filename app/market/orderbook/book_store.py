from __future__ import annotations

"""In-memory L2 order book store with diff application helpers."""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, Iterable, List, Mapping, MutableMapping, Tuple, TypedDict

from app.metrics.observability import set_market_data_staleness
from app.market.streams.base_ws import WsState


class DiffEvent(TypedDict):
    symbol: str
    bids: List[Tuple[float, float]]
    asks: List[Tuple[float, float]]
    seq_from: int
    seq_to: int
    ts_ms: int


@dataclass(slots=True)
class RingBuffer:
    """Lightweight ring buffer tracking the latest diff payloads."""

    capacity: int
    _queue: Deque[DiffEvent] = field(default_factory=deque)

    def append(self, event: DiffEvent) -> None:
        if self.capacity <= 0:
            return
        self._queue.append(event)
        while len(self._queue) > self.capacity:
            self._queue.popleft()

    def snapshot(self) -> List[DiffEvent]:
        return list(self._queue)


@dataclass(slots=True)
class BookSide:
    """Maintains sorted price levels for a single side of the book."""

    descending: bool
    levels: MutableMapping[float, float] = field(default_factory=dict)

    def apply(self, updates: Iterable[Tuple[float, float]]) -> None:
        for price, size in updates:
            price_f = float(price)
            size_f = float(size)
            if size_f <= 0:
                self.levels.pop(price_f, None)
            else:
                self.levels[price_f] = size_f

    def top(self) -> Tuple[float | None, float | None]:
        if not self.levels:
            return None, None
        if self.descending:
            price = max(self.levels)
        else:
            price = min(self.levels)
        return price, self.levels.get(price)

    def as_list(self, depth: int | None = None) -> List[Tuple[float, float]]:
        if not self.levels:
            return []
        prices = sorted(self.levels, reverse=self.descending)
        if depth is not None:
            prices = prices[:depth]
        return [(p, self.levels[p]) for p in prices]


@dataclass(slots=True)
class BookRecord:
    venue: str
    symbol: str
    bids: BookSide = field(default_factory=lambda: BookSide(descending=True))
    asks: BookSide = field(default_factory=lambda: BookSide(descending=False))
    last_applied_seq: int | None = None
    last_update_ts: float | None = None
    state: WsState = WsState.DOWN
    last_reason: str = ""
    resyncs: int = 0
    diff_history: RingBuffer = field(default_factory=lambda: RingBuffer(capacity=20))


class OrderBookStore:
    """Thread-safe order book cache supporting diff and snapshot application."""

    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._books: Dict[Tuple[str, str], BookRecord] = {}
        self._lock = threading.RLock()
        self._now = now or time.time

    def _key(self, venue: str, symbol: str) -> Tuple[str, str]:
        return (venue.lower(), symbol.upper())

    def get_or_create(self, venue: str, symbol: str) -> BookRecord:
        key = self._key(venue, symbol)
        with self._lock:
            record = self._books.get(key)
            if record is None:
                record = BookRecord(venue=venue, symbol=symbol)
                self._books[key] = record
            return record

    def apply_snapshot(
        self,
        *,
        venue: str,
        symbol: str,
        bids: Iterable[Tuple[float, float]],
        asks: Iterable[Tuple[float, float]],
        last_seq: int | None,
        ts_ms: int | None = None,
    ) -> None:
        record = self.get_or_create(venue, symbol)
        with self._lock:
            record.bids = BookSide(descending=True)
            record.asks = BookSide(descending=False)
            record.bids.apply(bids)
            record.asks.apply(asks)
            record.last_applied_seq = last_seq
            record.diff_history = RingBuffer(capacity=record.diff_history.capacity)
            timestamp = (ts_ms or 0) / 1000.0 if ts_ms else self._now()
            record.last_update_ts = timestamp
            record.state = WsState.CONNECTED
            record.last_reason = "snapshot"
            set_market_data_staleness(venue, symbol, 0.0)

    def apply_diff(self, venue: str, event: DiffEvent) -> None:
        record = self.get_or_create(venue, event["symbol"])
        with self._lock:
            seq_from = int(event["seq_from"])
            seq_to = int(event["seq_to"])
            last_seq = record.last_applied_seq
            if last_seq is not None and seq_from != last_seq + 1:
                raise ValueError(
                    f"non-monotonic diff for {venue}/{record.symbol}: expected {last_seq + 1}, got {seq_from}"
                )
            record.bids.apply(event.get("bids", []))
            record.asks.apply(event.get("asks", []))
            record.last_applied_seq = seq_to
            ts_ms = event.get("ts_ms")
            record.last_update_ts = (float(ts_ms) / 1000.0) if ts_ms else self._now()
            record.diff_history.append(event)
            record.state = WsState.CONNECTED
            record.last_reason = "diff"
            age = max(self._now() - (record.last_update_ts or self._now()), 0.0)
            set_market_data_staleness(venue, record.symbol, age)
            self._books[self._key(venue, record.symbol)] = record

    def record_resync(self, venue: str, symbol: str, reason: str) -> None:
        record = self.get_or_create(venue, symbol)
        with self._lock:
            record.resyncs += 1
            record.state = WsState.RESYNCING
            record.last_reason = reason

    def set_state(self, venue: str, symbol: str, state: WsState, reason: str | None = None) -> None:
        record = self.get_or_create(venue, symbol)
        with self._lock:
            record.state = state
            if reason:
                record.last_reason = reason

    def get_top_of_book(self, venue: str, symbol: str) -> Mapping[str, float | int | None]:
        record = self.get_or_create(venue, symbol)
        with self._lock:
            bid_price, bid_size = record.bids.top()
            ask_price, ask_size = record.asks.top()
            return {
                "bid": bid_price,
                "bid_size": bid_size,
                "ask": ask_price,
                "ask_size": ask_size,
                "seq": record.last_applied_seq,
            }

    def get_staleness_s(self, venue: str, symbol: str) -> float:
        record = self.get_or_create(venue, symbol)
        with self._lock:
            if record.last_update_ts is None:
                return float("inf")
            return max(self._now() - record.last_update_ts, 0.0)

    def status_snapshot(self) -> List[Dict[str, object]]:
        with self._lock:
            payload: List[Dict[str, object]] = []
            for record in self._books.values():
                payload.append(
                    {
                        "venue": record.venue,
                        "symbol": record.symbol,
                        "state": record.state.value,
                        "last_seq": record.last_applied_seq,
                        "staleness_s": self.get_staleness_s(record.venue, record.symbol),
                        "resyncs": record.resyncs,
                        "last_reason": record.last_reason,
                    }
                )
            return sorted(payload, key=lambda item: (item["venue"], item["symbol"]))


__all__ = [
    "DiffEvent",
    "OrderBookStore",
    "RingBuffer",
]
