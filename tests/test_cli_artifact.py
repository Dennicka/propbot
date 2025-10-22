from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from app import ledger


def test_cli_generates_plan_artifact(tmp_path):
    artifact = tmp_path / "last_plan.json"
    env = {**dict(os.environ), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run(
        ["python", "-m", "app.cli", "exec", "--profile", "paper", "--artifact", str(artifact)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert artifact.exists(), result.stderr
    payload = json.loads(artifact.read_text())
    assert payload["result"]["ok"] is True
    ledger.reset()


def test_cli_loop_cycles(tmp_path):
    ledger.reset()
    env = {**dict(os.environ), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    result = subprocess.run(
        [
            "python",
            "-m",
            "app.cli",
            "loop",
            "--env",
            "paper",
            "--pair",
            "BTCUSDT",
            "--venues",
            "binance-um",
            "okx-perp",
            "--notional",
            "25",
            "--cycles",
            "1",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    events = ledger.fetch_events(5)
    assert any(evt["code"] in {"loop_cycle", "loop_plan_unviable"} for evt in events)
    ledger.reset()
