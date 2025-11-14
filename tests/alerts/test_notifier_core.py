from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.alerts import notifier


class DummyThread:
    def __init__(self, target, daemon: bool = False) -> None:
        self._target = target
        self.daemon = daemon

    def start(self) -> None:  # pragma: no cover - thread suppressed
        return None


class CollectSink(notifier.Sink):
    def __init__(self) -> None:
        self.events: list[notifier.Event] = []

    def send(self, event: notifier.Event) -> bool:
        self.events.append(event)
        return True


class DummyBucket:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def consume(self) -> bool:
        return self.allowed


@pytest.fixture(autouse=True)
def _disable_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notifier.threading, "Thread", DummyThread)


def _gauge_value() -> float:
    return notifier._ALERTS_QUEUE_GAUGE._values.get((), 0.0)


def _counter_value(counter, *labels: str) -> float:
    return counter._values.get(tuple(labels), 0.0)


def test_parse_rate_limit_variants() -> None:
    assert notifier._parse_rate_limit("5/min") == pytest.approx(5.0 / 60.0)
    assert notifier._parse_rate_limit("10/sec") == pytest.approx(10.0)
    assert notifier._parse_rate_limit("7") == pytest.approx(7.0)
    assert notifier._parse_rate_limit("invalid") == pytest.approx(5.0 / 60.0)


def test_token_bucket_refill(monkeypatch: pytest.MonkeyPatch) -> None:
    now = {"value": 0.0}

    def fake_monotonic() -> float:
        return now["value"]

    monkeypatch.setattr(notifier.time, "monotonic", fake_monotonic)
    bucket = notifier.TokenBucket(rate_per_second=1.0, capacity=2)
    assert bucket.consume() is True
    assert bucket.consume() is True
    assert bucket.consume() is False
    now["value"] = 1.1
    assert bucket.consume() is True


def test_queue_overflow_and_gauge(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = DummyBucket()
    alert_notifier = notifier.MultiNotifier(bucket=bucket, queue_max=2, include=None)
    sink = CollectSink()
    alert_notifier.add_sink("collect", sink)
    before = _counter_value(notifier._ALERTS_DROPPED_TOTAL, "queue_full")
    alert_notifier.emit(notifier.Event(kind="router-block", severity="warn", title="one"))
    alert_notifier.emit(notifier.Event(kind="router-block", severity="warn", title="two"))
    alert_notifier.emit(notifier.Event(kind="router-block", severity="warn", title="three"))
    assert len(alert_notifier._queue) == 2
    assert alert_notifier._queue[0].title == "two"
    after = _counter_value(notifier._ALERTS_DROPPED_TOTAL, "queue_full")
    assert after == pytest.approx(before + 1.0)
    assert _gauge_value() == pytest.approx(2.0)
    alert_notifier.drain_once()
    assert [event.title for event in sink.events] == ["two", "three"]


def test_include_filter_blocks_kind() -> None:
    bucket = DummyBucket()
    alert_notifier = notifier.MultiNotifier(bucket=bucket, queue_max=10, include={"pnl-cap"})
    alert_notifier.emit(notifier.Event(kind="router-block", severity="warn", title="skip"))
    assert len(alert_notifier._queue) == 0
    assert _gauge_value() == pytest.approx(0.0)


def test_file_sink_writes_json(tmp_path: Path) -> None:
    sink_path = tmp_path / "alerts.log"
    sink = notifier.FileSink(str(sink_path))
    event = notifier.Event(
        kind="router-block",
        severity="warn",
        title="block",
        detail="detail",
        tags={"reason": "risk"},
    )
    assert sink.send(event) is True
    contents = sink_path.read_text(encoding="utf-8").strip()
    payload = json.loads(contents)
    assert payload["title"] == "block"
    assert payload["tags"]["reason"] == "risk"


def test_rate_limit_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = DummyBucket(allowed=False)
    alert_notifier = notifier.MultiNotifier(bucket=bucket, queue_max=10, include=None)
    sink = CollectSink()
    alert_notifier.add_sink("collect", sink)
    before = _counter_value(notifier._ALERTS_DROPPED_TOTAL, "rate_limit")
    alert_notifier.emit(notifier.Event(kind="router-block", severity="warn", title="blocked"))
    alert_notifier.drain_once()
    after = _counter_value(notifier._ALERTS_DROPPED_TOTAL, "rate_limit")
    assert after == pytest.approx(before + 1.0)
    assert sink.events == []
    assert _gauge_value() == pytest.approx(0.0)
