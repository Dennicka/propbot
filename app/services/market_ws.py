from __future__ import annotations

"""Singleton helpers for the websocket market data layer."""

import threading
from typing import List

from app.market.orderbook.book_store import OrderBookStore

_LOCK = threading.Lock()
_STORE: OrderBookStore | None = None


def get_orderbook_store() -> OrderBookStore:
    global _STORE
    with _LOCK:
        if _STORE is None:
            _STORE = OrderBookStore()
        return _STORE


def reset_for_tests() -> None:
    global _STORE
    with _LOCK:
        _STORE = OrderBookStore()


def market_status_snapshot() -> List[dict[str, object]]:
    store = get_orderbook_store()
    return store.status_snapshot()


__all__ = ["get_orderbook_store", "market_status_snapshot", "reset_for_tests"]
