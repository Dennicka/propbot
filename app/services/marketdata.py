"""Market data aggregation utilities for combining websocket and REST feeds."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, MutableMapping, Tuple


BookFetcher = Callable[[str], Dict[str, float]]


@dataclass
class _BookEntry:
    bid: float
    ask: float
    ts: float
    source: str


class MarketDataAggregator:
    """Simple best bid/ask cache fed by websocket snapshots with REST fallback."""

    def __init__(
        self,
        *,
        rest_fetchers: MutableMapping[str, BookFetcher] | None = None,
        stale_after: float = 1.0,
    ) -> None:
        self._rest_fetchers: MutableMapping[str, BookFetcher] = rest_fetchers or {}
        self._stale_after = float(stale_after)
        self._books: Dict[Tuple[str, str], _BookEntry] = {}
        self._lock = threading.Lock()

    def register_rest_fetcher(self, venue: str, fetcher: BookFetcher) -> None:
        self._rest_fetchers[venue.lower()] = fetcher

    def update_from_ws(
        self,
        *,
        venue: str,
        symbol: str,
        bid: float,
        ask: float,
        ts: float | None = None,
    ) -> None:
        ts_value = float(ts) if ts is not None else time.time()
        key = (venue.lower(), symbol.upper())
        entry = _BookEntry(bid=float(bid), ask=float(ask), ts=ts_value, source="ws")
        with self._lock:
            self._books[key] = entry

    def _fetch_via_rest(self, venue: str, symbol: str) -> _BookEntry:
        fetcher = self._rest_fetchers.get(venue.lower())
        if not fetcher:
            raise KeyError(f"no market data fetcher registered for venue {venue}")
        payload = fetcher(symbol)
        bid = float(payload.get("bid", 0.0))
        ask = float(payload.get("ask", 0.0))
        ts_value = float(payload.get("ts", time.time()))
        entry = _BookEntry(bid=bid, ask=ask, ts=ts_value, source="rest")
        with self._lock:
            self._books[(venue.lower(), symbol.upper())] = entry
        return entry

    def top_of_book(self, venue: str, symbol: str) -> Dict[str, float]:
        key = (venue.lower(), symbol.upper())
        now = time.time()
        with self._lock:
            entry = self._books.get(key)
        if entry is None or (self._stale_after and now - entry.ts > self._stale_after):
            entry = self._fetch_via_rest(venue, symbol)
        return {"bid": entry.bid, "ask": entry.ask, "ts": entry.ts}


__all__ = ["MarketDataAggregator"]

