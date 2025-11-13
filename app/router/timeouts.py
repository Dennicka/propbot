from __future__ import annotations
from dataclasses import dataclass
from time import time
from typing import Dict, Literal, Optional

Phase = Literal["submit", "ack", "fill"]


@dataclass
class _DL:
    ack_deadline: float = 0.0
    fill_deadline: float = 0.0
    phase: Phase = "submit"


class DeadlineTracker:
    def __init__(self, ack_sec: int = 3, fill_sec: int = 30, max_items: int = 20_000):
        self._ack = ack_sec
        self._fill = fill_sec
        self._max = max_items
        self._d: Dict[str, _DL] = {}

    def on_submit(self, order_id: str, now: Optional[float] = None) -> None:
        t = now or time()
        self._d[order_id] = _DL(
            ack_deadline=t + self._ack,
            fill_deadline=t + self._fill,
            phase="submit",
        )
        self._cap()

    def on_ack(
        self, order_id: str, now: Optional[float] = None
    ) -> None:  # noqa: ARG002 - time reserved for future use
        if order_id in self._d:
            self._d[order_id].phase = "ack"

    def on_fill_progress(
        self, order_id: str, now: Optional[float] = None
    ) -> None:  # noqa: ARG002 - time reserved for future use
        if order_id in self._d:
            self._d[order_id].phase = "fill"

    def done(self, order_id: str) -> None:
        self._d.pop(order_id, None)

    def due_to_expire(self, now: Optional[float] = None) -> Dict[str, str]:
        t = now or time()
        out: Dict[str, str] = {}
        for oid, dl in list(self._d.items()):
            if dl.phase == "submit" and t >= dl.ack_deadline:
                out[oid] = "ack-timeout"
            elif dl.phase in ("ack", "fill") and t >= dl.fill_deadline:
                out[oid] = "fill-timeout"
        return out

    def _cap(self) -> None:
        n = len(self._d) - self._max
        if n > 0:
            for oid, _ in sorted(self._d.items(), key=lambda kv: kv[1].ack_deadline)[:n]:
                self._d.pop(oid, None)
