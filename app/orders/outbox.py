from dataclasses import dataclass
from time import time
from typing import Dict, Literal, Optional

State = Literal["pending", "acked", "terminal"]


@dataclass
class _Entry:
    state: State
    ts: float


class Outbox:
    def __init__(self, ttl_seconds: int = 1800, max_items: int = 10000):
        self._ttl = ttl_seconds
        self._max = max_items
        self._box: Dict[str, _Entry] = {}
        self.stats = {"seen": 0, "skip_duplicate": 0, "ack": 0, "terminal": 0, "ttl": 0, "size": 0}

    def should_send(self, key: str, now: Optional[float] = None) -> bool:
        """Регистрирует попытку отправки. Возвращает False, если уже pending/acked."""

        now = now or time()
        self.stats["seen"] += 1
        entry = self._box.get(key)
        if entry and entry.state in ("pending", "acked"):
            self.stats["skip_duplicate"] += 1
            entry.ts = now
            return False
        self._box[key] = _Entry("pending", now)
        return True

    def mark_acked(self, key: str, now: Optional[float] = None) -> None:
        now = now or time()
        entry = self._box.get(key)
        if entry:
            entry.state, entry.ts = "acked", now
        else:
            self._box[key] = _Entry("acked", now)
        self.stats["ack"] += 1

    def mark_terminal(self, key: str) -> None:
        if key in self._box:
            del self._box[key]
        self.stats["terminal"] += 1

    def cleanup(self, now: Optional[float] = None) -> None:
        now = now or time()
        # TTL
        to_remove = [k for k, v in self._box.items() if now - v.ts > self._ttl]
        for key in to_remove:
            del self._box[key]
        if to_remove:
            self.stats["ttl"] += len(to_remove)
        # SIZE
        if len(self._box) > self._max:
            # удалить самые старые
            excess = len(self._box) - self._max
            for key, _ in sorted(self._box.items(), key=lambda kv: kv[1].ts)[:excess]:
                del self._box[key]
                self.stats["size"] += 1
