import json

import pytest

from app.cli_golden import GoldenCheckResult, run_check, snapshot_baseline


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def test_snapshot_creates_baseline(tmp_path):
    current_path = tmp_path / "current.jsonl"
    baseline_path = tmp_path / "baseline.jsonl"
    records = [
        {"event": "route_decision", "payload": {"symbol": "BTC", "score": 1.0}, "ts": 1.0},
        {"event": "order_submit", "payload": {"intent_id": "123", "qty": 1}, "ts": 2.0},
    ]
    _write_jsonl(current_path, records)

    snapshot_baseline(current_path=current_path, baseline_path=baseline_path)

    assert baseline_path.exists()
    baseline_records = [
        json.loads(line) for line in baseline_path.read_text().splitlines() if line.strip()
    ]
    assert all("ts" not in entry.get("payload", {}) for entry in baseline_records)
    assert {entry["event"] for entry in baseline_records} == {"route_decision", "order_submit"}


def test_run_check_passes_when_matching(tmp_path):
    current_path = tmp_path / "current.jsonl"
    baseline_path = tmp_path / "baseline.jsonl"
    records = [
        {"event": "health_guard", "payload": {"state": "OK"}},
    ]
    _write_jsonl(current_path, records)
    snapshot_baseline(current_path=current_path, baseline_path=baseline_path)

    result = run_check(current_path=current_path, baseline_path=baseline_path)
    assert isinstance(result, GoldenCheckResult)
    assert result.matched


def test_run_check_detects_difference(tmp_path):
    current_path = tmp_path / "current.jsonl"
    baseline_path = tmp_path / "baseline.jsonl"
    base_records = [{"event": "safety_hold", "payload": {"reason": "x"}}]
    _write_jsonl(current_path, base_records)
    snapshot_baseline(current_path=current_path, baseline_path=baseline_path)

    new_records = [{"event": "safety_hold", "payload": {"reason": "y"}}]
    _write_jsonl(current_path, new_records)

    result = run_check(current_path=current_path, baseline_path=baseline_path)
    assert not result.matched
    assert "baseline" in result.message


def test_run_check_skips_when_baseline_missing(tmp_path):
    current_path = tmp_path / "current.jsonl"
    _write_jsonl(current_path, [{"event": "noop", "payload": {}}])

    result = run_check(current_path=current_path, baseline_path=tmp_path / "missing.jsonl")
    assert result.matched
    assert "baseline missing" in result.message
