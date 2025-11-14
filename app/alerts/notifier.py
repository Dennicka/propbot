from __future__ import annotations

import json
import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Callable, Deque, Dict

from app.metrics.core import counter, gauge, histogram

LOGGER = logging.getLogger(__name__)


@dataclass
class Event:
    """Structured notification payload."""

    kind: str
    severity: str
    title: str
    detail: str = ""
    ts: float = 0.0
    tags: Dict[str, str] | None = None
    ctx: Dict[str, str] | None = None

    def ensure_timestamp(self) -> None:
        if self.ts <= 0.0:
            self.ts = time.time()


class TokenBucket:
    """Simple token bucket rate limiter."""

    def __init__(self, rate_per_second: float, capacity: int) -> None:
        self._rate = max(0.0, rate_per_second)
        self._capacity = max(0, capacity)
        self._tokens = float(self._capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill_locked()
            if self._tokens < tokens:
                return False
            self._tokens -= tokens
            return True

    def _refill_locked(self) -> None:
        now = time.monotonic()
        delta = now - self._updated
        if delta <= 0.0 or self._rate <= 0.0:
            self._updated = now
            return
        self._tokens = min(self._capacity, self._tokens + delta * self._rate)
        self._updated = now


class Sink(ABC):
    @abstractmethod
    def send(self, event: Event) -> bool:
        """Deliver an event payload to the sink."""


class StdoutSink(Sink):
    def __init__(self, stream: Callable[[str], None] | None = None) -> None:
        self._stream = stream or print

    def send(self, event: Event) -> bool:
        payload = _event_to_json(event)
        self._stream(payload)
        return True


class FileSink(Sink):
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()

    def send(self, event: Event) -> bool:
        line = _event_to_json(event)
        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with self._lock:
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    try:
                        os.fsync(handle.fileno())
                    except OSError:
                        LOGGER.debug("alerts.filesink.fsync_failed", exc_info=True)
        except OSError:
            LOGGER.exception("alerts.filesink.write_failed path=%s", self._path)
            return False
        return True


class TelegramSink(Sink):
    def __init__(self, sender: Callable[[str], bool]) -> None:
        self._sender = sender

    def send(self, event: Event) -> bool:
        text = _format_markdown(event)
        return self._sender(text)


_ALERTS_EMITTED_TOTAL = counter("propbot_alerts_emitted_total", labels=("kind", "severity"))
_ALERTS_SENT_TOTAL = counter("propbot_alerts_sent_total", labels=("sink", "status"))
_ALERTS_DROPPED_TOTAL = counter("propbot_alerts_dropped_total", labels=("reason",))
_ALERTS_QUEUE_GAUGE = gauge("propbot_alerts_queue")
_ALERTS_SEND_MS = histogram("propbot_alerts_send_ms")


def _event_to_json(event: Event) -> str:
    record = asdict(event)
    return json.dumps(record, ensure_ascii=False, default=_json_default, sort_keys=True)


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _escape_markdown(value: str) -> str:
    replacements = {
        "_": "\\_",
        "*": "\\*",
        "[": "\\[",
        "]": "\\]",
        "(": "\\(",
        ")": "\\)",
        "#": "\\#",
        "+": "\\+",
        "-": "\\-",
        "=": "\\=",
        "|": "\\|",
        "{": "\\{",
        "}": "\\}",
        "!": "\\!",
    }
    return "".join(replacements.get(ch, ch) for ch in value)


def _format_markdown(event: Event) -> str:
    title = _escape_markdown(event.title)
    lines = [f"*{title}*"]
    lines.append(f"`{_escape_markdown(event.kind)}` · `{_escape_markdown(event.severity)}`")
    if event.detail:
        lines.append(_escape_markdown(event.detail))
    if event.tags:
        tag_parts = [f"{key}={value}" for key, value in event.tags.items()]
        lines.append("tags: " + _escape_markdown(", ".join(tag_parts)))
    if event.ctx:
        ctx_parts = [f"{key}={value}" for key, value in event.ctx.items()]
        lines.append("ctx: " + _escape_markdown(", ".join(ctx_parts)))
    return "\n".join(lines)


def _parse_rate_limit(raw: str | None) -> float:
    if not raw:
        return 5.0 / 60.0
    chunk = raw.strip().lower()
    if "/" not in chunk:
        try:
            value = float(chunk)
        except ValueError:
            return 5.0 / 60.0
        return max(0.0, value)
    number, period = chunk.split("/", 1)
    try:
        tokens = float(number)
    except ValueError:
        tokens = 5.0
    period = period.strip()
    if period == "sec":
        return max(0.0, tokens)
    if period == "min":
        return max(0.0, tokens / 60.0)
    return max(0.0, tokens)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning("alerts.invalid_int_env name=%s raw=%r", name, raw)
        return default


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_retry(raw: str | None) -> list[int]:
    if not raw:
        return []
    values: list[int] = []
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            delay = int(token)
        except ValueError:
            continue
        if delay >= 0:
            values.append(delay)
    return values


def _parse_include(raw: str | None) -> set[str] | None:
    if not raw or raw.strip().lower() == "all":
        return None
    allowed: set[str] = set()
    for chunk in raw.split(","):
        token = chunk.strip()
        if token:
            allowed.add(token)
    return allowed


class MultiNotifier:
    def __init__(
        self,
        bucket: TokenBucket,
        queue_max: int,
        include: set[str] | None,
    ) -> None:
        self._bucket = bucket
        self._queue: Deque[Event] = deque()
        self._queue_max = max(1, queue_max)
        self._include = include
        self._sinks: Dict[str, Sink] = {}
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def add_sink(self, name: str, sink: Sink) -> None:
        self._sinks[name] = sink

    def emit(self, event: Event) -> None:
        if self._include is not None and event.kind not in self._include:
            return
        event.ensure_timestamp()
        with self._lock:
            self._queue.append(event)
            dropped = 0
            while len(self._queue) > self._queue_max:
                self._queue.popleft()
                dropped += 1
            if dropped:
                _ALERTS_DROPPED_TOTAL.labels(reason="queue_full").inc(float(dropped))
            _ALERTS_QUEUE_GAUGE.set(float(len(self._queue)))
            self._condition.notify_all()
        _ALERTS_EMITTED_TOTAL.labels(kind=event.kind, severity=event.severity).inc()

    def drain_once(self) -> None:
        while True:
            event = self._next_event()
            if event is None:
                return
            if not self._bucket.consume():
                _ALERTS_DROPPED_TOTAL.labels(reason="rate_limit").inc()
                continue
            self._deliver(event)

    def _next_event(self) -> Event | None:
        with self._lock:
            if not self._queue:
                return None
            event = self._queue.popleft()
            _ALERTS_QUEUE_GAUGE.set(float(len(self._queue)))
            return event

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                while not self._queue:
                    self._condition.wait()
            self.drain_once()

    def _deliver(self, event: Event) -> None:
        for name, sink in list(self._sinks.items()):
            start = time.monotonic()
            status = "ok"
            try:
                if not sink.send(event):
                    status = "fail"
            except Exception:  # pragma: no cover - defensive logging
                LOGGER.exception("alerts.sink_failed sink=%s", name)
                status = "fail"
            finally:
                elapsed_ms = (time.monotonic() - start) * 1000.0
                _ALERTS_SEND_MS.observe(elapsed_ms)
                _ALERTS_SENT_TOTAL.labels(sink=name, status=status).inc()


def _build_notifier() -> MultiNotifier:
    rate = _parse_rate_limit(os.getenv("ALERTS_RATE_LIMIT"))
    burst = _env_int("ALERTS_BURST", 10)
    queue_max = _env_int("ALERTS_QUEUE_MAX", 1000)
    include = _parse_include(os.getenv("ALERTS_INCLUDE"))
    bucket = TokenBucket(rate, burst)
    notifier = MultiNotifier(bucket=bucket, queue_max=queue_max, include=include)
    notifier.add_sink("stdout", StdoutSink())
    file_path = os.getenv("ALERTS_FILE_PATH", "data/alerts.log")
    if file_path:
        notifier.add_sink("file", FileSink(file_path))
    if _env_flag("FF_ALERTS_TELEGRAM"):
        token = os.getenv("ALERTS_TG_BOT_TOKEN", "")
        chat_id = os.getenv("ALERTS_TG_CHAT_ID", "")
        timeout = _env_int("ALERTS_TG_TIMEOUT_SEC", 5)
        retries = _parse_retry(os.getenv("ALERTS_TG_RETRY"))
        if token and chat_id:
            from . import wire_telegram

            delays = [0.0, *(float(d) for d in retries)] if retries else [0.0]

            def _send(text: str) -> bool:
                extra = {"disable_web_page_preview": "true"}
                for delay in delays:
                    if delay > 0:
                        time.sleep(delay)
                    try:
                        status = wire_telegram.send_message(
                            token=token,
                            chat_id=chat_id,
                            text=text,
                            timeout=float(timeout),
                            extra=extra,
                        )
                    except wire_telegram.TelegramWireError as exc:
                        LOGGER.warning(
                            "alerts.telegram_send_retry",
                            extra={"delay": delay, "error": str(exc)},
                        )
                        continue
                    return 200 <= status < 300
                LOGGER.warning("alerts.telegram_send_failed")
                return False

            notifier.add_sink("telegram", TelegramSink(_send))
        else:
            LOGGER.warning("alerts.telegram_not_configured")
    return notifier


_NOTIFIER: MultiNotifier | None = None
_NOTIFIER_LOCK = threading.Lock()


def get_notifier() -> MultiNotifier:
    global _NOTIFIER
    if _NOTIFIER is None:
        with _NOTIFIER_LOCK:
            if _NOTIFIER is None:
                _NOTIFIER = _build_notifier()
    return _NOTIFIER
