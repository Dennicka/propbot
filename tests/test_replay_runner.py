from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.tools import replay_runner


def _write_sample(path: Path) -> None:
    path.write_text(
        """
{"ts": "2024-01-01T00:00:00Z", "symbol": "BTCUSDT", "side": "buy", "status": "filled", "qty": 0.01, "quote_price": 20000.0, "fill_price": 20001.0, "fees_usd": 0.05}
{"ts": "2024-01-01T00:01:00Z", "symbol": "BTCUSDT", "side": "sell", "status": "filled", "qty": 0.01, "quote_price": 20010.0, "fill_price": 20009.0, "fees_usd": 0.05}
{"ts": "2024-01-01T00:02:00Z", "symbol": "BTCUSDT", "side": "buy", "status": "rejected", "qty": 0.01, "quote_price": 20005.0}
""".strip()
        + "\n"
    )


def test_replay_runner_summary(tmp_path):
    sample = tmp_path / "sample.jsonl"
    _write_sample(sample)

    records = list(replay_runner.load_records(sample))
    summary = replay_runner.compute_summary(records, input_file=str(sample))

    assert summary.attempts == 3
    assert summary.fills == 2
    assert summary.hit_ratio == pytest.approx(2 / 3)
    assert summary.gross_pnl == pytest.approx(-0.02)
    assert summary.fees_total == pytest.approx(0.1)
    assert summary.net_pnl == pytest.approx(-0.12)
    assert summary.total_notional == pytest.approx(400.1)
    assert summary.avg_slippage_bps == pytest.approx(-200.0 / 400.1)

    outdir = tmp_path / "reports"
    timestamp = datetime(2024, 1, 2, 3, 4, tzinfo=timezone.utc)
    json_path, csv_path = replay_runner.save_summary(
        summary, output_dir=outdir, timestamp=timestamp
    )

    assert json_path.name == "backtest_20240102_0304.json"
    assert csv_path.name == "backtest_20240102_0304.csv"

    payload = json.loads(json_path.read_text())
    assert payload["attempts"] == 3
    assert payload["fills"] == 2
    assert payload["gross_pnl"] == pytest.approx(-0.02)
    assert payload["net_pnl"] == pytest.approx(-0.12)

    csv_content = csv_path.read_text().splitlines()
    assert csv_content[0].startswith("input_file,")
    assert sample.name in csv_content[1]
    assert "-0.12" in csv_content[1]


def test_run_backtest_creates_summary(tmp_path, monkeypatch):
    sample = tmp_path / "sample.jsonl"
    _write_sample(sample)
    reports_dir = tmp_path / "reports"

    monkeypatch.chdir(tmp_path)
    summary = replay_runner.run_backtest(sample, output_dir=reports_dir)
    assert summary.fills == 2
    outputs = sorted(reports_dir.glob("backtest_*.json"))
    assert outputs, "report json should be generated"

