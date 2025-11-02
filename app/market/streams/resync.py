from __future__ import annotations

"""Shared helpers for venue specific websocket resync flows."""

import logging
import time
from collections import defaultdict
from typing import Callable, DefaultDict, Iterable, Mapping

from .base_ws import GapDetector, WsConnector, WsState
from ..orderbook.book_store import DiffEvent, OrderBookStore

logger = logging.getLogger(__name__)


class BaseOrderBookStream:
    """Common logic for websocket driven order book maintenance."""

    def __init__(
        self,
        *,
        venue: str,
        orderbook: OrderBookStore,
        connector: WsConnector,
        snapshot_fetcher: Callable[[str], Mapping[str, object]],
        gap_detector_factory: Callable[[str], GapDetector] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.venue = venue
        self._orderbook = orderbook
        self._connector = connector
        self._snapshot_fetcher = snapshot_fetcher
        self._gap_factory = gap_detector_factory or (
            lambda symbol: GapDetector(venue=venue, symbol=symbol)
        )
        self._gaps: dict[str, GapDetector] = {}
        self._ready: dict[str, bool] = defaultdict(lambda: False)
        self._pending: DefaultDict[str, list[DiffEvent]] = defaultdict(list)
        self._clock = clock or time.monotonic

    def _gap(self, symbol: str) -> GapDetector:
        detector = self._gaps.get(symbol)
        if detector is None:
            detector = self._gap_factory(symbol)
            self._gaps[symbol] = detector
        return detector

    # -- hooks for subclasses -------------------------------------------------
    def _parse_snapshot(
        self, symbol: str, snapshot: Mapping[str, object]
    ) -> tuple[Iterable[tuple[float, float]], Iterable[tuple[float, float]], int, int | None]:
        raise NotImplementedError

    def _validate_diff(self, symbol: str, event: DiffEvent) -> bool:
        return True

    # -- public API -----------------------------------------------------------
    def handle_snapshot(self, symbol: str, snapshot: Mapping[str, object]) -> None:
        bids, asks, last_seq, ts_ms = self._parse_snapshot(symbol, snapshot)
        self._orderbook.apply_snapshot(
            venue=self.venue,
            symbol=symbol,
            bids=bids,
            asks=asks,
            last_seq=last_seq,
            ts_ms=ts_ms,
        )
        detector = self._gap(symbol)
        detector.reset(last_seq)
        self._ready[symbol] = True
        self._drain_pending(symbol)
        self._connector.transition(WsState.CONNECTED, reason="snapshot_applied")

    def handle_diff(self, event: DiffEvent) -> None:
        symbol = event["symbol"]
        self._connector.on_message(ts=(event.get("ts_ms") or 0) / 1000.0)
        if not self._ready[symbol]:
            self._pending[symbol].append(event)
            return
        if not self._validate_diff(symbol, event):
            self._resync(symbol, reason="validation_failed")
            return
        gap_event = self._gap(symbol).observe(event["seq_from"], event["seq_to"])
        if gap_event is not None:
            self._resync(symbol, reason="gap_detected")
            return
        try:
            self._orderbook.apply_diff(self.venue, event)
        except ValueError:
            logger.exception(
                "orderbook.diff.apply_failed",
                extra={"venue": self.venue, "symbol": symbol},
            )
            self._resync(symbol, reason="apply_failed")
            return
        self._connector.transition(WsState.CONNECTED, reason="diff")

    # -- helpers --------------------------------------------------------------
    def _drain_pending(self, symbol: str) -> None:
        if not self._pending[symbol]:
            return
        queue = list(self._pending.pop(symbol))
        for event in queue:
            self.handle_diff(event)

    def _resync(self, symbol: str, *, reason: str) -> None:
        start = self._clock()
        logger.info(
            "orderbook.resync.start",
            extra={"venue": self.venue, "symbol": symbol, "reason": reason},
        )
        self._connector.transition(WsState.RESYNCING, reason=reason)
        self._orderbook.record_resync(self.venue, symbol, reason)
        self._connector.mark_resync(symbol)
        try:
            snapshot = self._snapshot_fetcher(symbol)
        except Exception:
            logger.exception(
                "orderbook.resync.snapshot_failed",
                extra={"venue": self.venue, "symbol": symbol, "reason": reason},
            )
            self._connector.reconnect_now(reason)
            return
        bids, asks, last_seq, ts_ms = self._parse_snapshot(symbol, snapshot)
        self._orderbook.apply_snapshot(
            venue=self.venue,
            symbol=symbol,
            bids=bids,
            asks=asks,
            last_seq=last_seq,
            ts_ms=ts_ms,
        )
        self._gap(symbol).reset(last_seq)
        self._ready[symbol] = True
        duration = max(self._clock() - start, 0.0)
        logger.info(
            "orderbook.resync.finished",
            extra={
                "venue": self.venue,
                "symbol": symbol,
                "reason": reason,
                "duration_ms": int(duration * 1000),
                "snapshot_last_seq": last_seq,
            },
        )
        self._drain_pending(symbol)
        self._connector.transition(WsState.CONNECTED, reason="resync_complete")


__all__ = ["BaseOrderBookStream"]
