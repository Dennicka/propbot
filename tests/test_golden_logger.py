import json

from app.golden.logger import GoldenEventLogger, normalise_events


def test_logger_writes_when_enabled(tmp_path):
    log_path = tmp_path / "current.jsonl"
    logger = GoldenEventLogger(enabled=True, path=log_path)
    logger.log("order_submit", {"foo": "bar", "qty": 1})
    assert log_path.exists()
    payloads = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert payloads
    record = payloads[0]
    assert record["event"] == "order_submit"
    assert record["payload"]["foo"] == "bar"
    assert "ts" in record


def test_logger_disabled_does_not_write(tmp_path):
    log_path = tmp_path / "disabled.jsonl"
    logger = GoldenEventLogger(enabled=False, path=log_path)
    logger.log("noop", {"value": 1})
    assert not log_path.exists()


def test_normalise_events_strips_volatile_fields():
    records = [
        {
            "event": "freeze_applied",
            "payload": {
                "reason": "TEST::scope",
                "ts": 123.0,
                "nested": {"id": "abc", "keep": 42},
            },
            "ts": 456.0,
            "id": "volatile",
        }
    ]
    normalised = normalise_events(records)
    assert normalised == [
        {"event": "freeze_applied", "payload": {"nested": {"keep": 42}, "reason": "TEST::scope"}}
    ]
