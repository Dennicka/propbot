from __future__ import annotations

import io
import json
import os
import pathlib
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Dict, Iterable, Optional, Tuple

from app.metrics.core import counter as metrics_counter


@dataclass
class OutboxRecord:
    ts: float
    status: str
    intent_key: str
    order_id: str
    strategy: str
    symbol: str
    venue: str
    side: str
    qty: str
    px: str
    reason: str = ""
    exch_order_id: str = ""


_OUTBOX_WRITE_TOTAL = metrics_counter("propbot_outbox_write_total")


class OutboxJournal:
    def __init__(
        self,
        path: str,
        rotate_mb: int = 8,
        flush_every: int = 1,
        dupe_window_sec: int = 10,
        max_inmem: int = 200_000,
    ) -> None:
        self._path = pathlib.Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_bytes = max(0, int(rotate_mb)) * 1024 * 1024
        self._flush_every = max(1, int(flush_every))
        self._dupe_window = max(0, int(dupe_window_sec))
        self._max_inmem = max_inmem
        self._fp: Optional[io.TextIOWrapper] = None
        self._wcount = 0
        self._by_intent: Dict[str, Tuple[float, str, str]] = {}
        self._by_order: Dict[str, str] = {}
        self._load_existing()

    def begin_pending(
        self,
        *,
        intent_key: str,
        order_id: str,
        strategy: str,
        symbol: str,
        venue: str,
        side: str,
        qty: Decimal,
        px: Decimal,
    ) -> None:
        record = OutboxRecord(
            ts=time.time(),
            status="PENDING",
            intent_key=intent_key,
            order_id=order_id,
            strategy=strategy,
            symbol=symbol,
            venue=venue,
            side=side,
            qty=str(qty),
            px=str(px),
        )
        self._append(record)

    def mark_acked(self, order_id: str, exch_order_id: str = "") -> None:
        self._append_status(order_id, "ACKED", exch_order_id, "")

    def mark_final(self, order_id: str, reason: str = "") -> None:
        record_reason = reason or ""
        self._append_status(order_id, "FINAL", "", record_reason)

    def mark_failed(self, order_id: str, reason: str) -> None:
        self._append_status(order_id, "FAILED", "", reason)

    def last_by_intent(self, intent_key: str) -> Optional[Tuple[float, str, str]]:
        return self._by_intent.get(intent_key)

    def status_by_order(self, order_id: str) -> Optional[str]:
        return self._by_order.get(order_id)

    def iter_replay_candidates(
        self,
        *,
        now: Optional[float] = None,
        min_age_sec: int = 5,
    ) -> Iterable[OutboxRecord]:
        current = now or time.time()
        min_age = max(0, int(min_age_sec))
        for record in self._iter_file():
            if record.status != "PENDING":
                continue
            if current - record.ts < float(min_age):
                continue
            status = self._by_order.get(record.order_id, "")
            if status in {"ACKED", "FINAL", "FAILED"}:
                continue
            yield record

    def _append_status(
        self,
        order_id: str,
        status: str,
        exch_order_id: str,
        reason: str,
    ) -> None:
        record = OutboxRecord(
            ts=time.time(),
            status=status,
            intent_key=self._intent_for_order(order_id),
            order_id=order_id,
            strategy="",
            symbol="",
            venue="",
            side="",
            qty="0",
            px="0",
            reason=reason,
            exch_order_id=exch_order_id,
        )
        self._append(record)

    def _intent_for_order(self, order_id: str) -> str:
        for key, value in self._by_intent.items():
            _, _, known_order = value
            if known_order == order_id:
                return key
        return ""

    def _append(self, record: OutboxRecord) -> None:
        handle = self._open_fp()
        payload = json.dumps(asdict(record), ensure_ascii=False)
        handle.write(payload + "\n")
        self._wcount += 1
        self._update_index(record)
        _OUTBOX_WRITE_TOTAL.inc()
        if self._wcount % self._flush_every == 0:
            handle.flush()
            os.fsync(handle.fileno())
        self._maybe_rotate(handle)

    def _open_fp(self) -> io.TextIOWrapper:
        if self._fp is None:
            self._fp = open(self._path, "a", encoding="utf-8")
        return self._fp

    def _maybe_rotate(self, handle: io.TextIOWrapper) -> None:
        try:
            if self._path.exists() and self._path.stat().st_size >= self._rotate_bytes:
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
                self._fp = None
                rotated = self._path.with_suffix(self._path.suffix + f".{int(time.time())}")
                os.replace(self._path, rotated)
        except OSError:
            return

    def _update_index(self, record: OutboxRecord) -> None:
        if len(self._by_intent) > self._max_inmem:
            oldest = sorted(self._by_intent.items(), key=lambda item: item[1][0])[
                : max(1, len(self._by_intent) // 2)
            ]
            for key, _ in oldest:
                self._by_intent.pop(key, None)
        if record.intent_key:
            last = self._by_intent.get(record.intent_key)
            if last is None or record.status != "PENDING" or record.ts >= last[0]:
                self._by_intent[record.intent_key] = (
                    record.ts,
                    record.status,
                    record.order_id,
                )
        if record.order_id:
            self._by_order[record.order_id] = record.status

    def _iter_file(self) -> Iterable[OutboxRecord]:
        yield from self._read_one(self._path)
        pattern = f"{self._path.name}.*"
        for candidate in sorted(self._path.parent.glob(pattern)):
            yield from self._read_one(candidate)

    def _read_one(self, path: pathlib.Path) -> Iterable[OutboxRecord]:
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    payload = json.loads(line)
                    yield OutboxRecord(**payload)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue

    def _load_existing(self) -> None:
        for record in self._iter_file():
            self._update_index(record)
