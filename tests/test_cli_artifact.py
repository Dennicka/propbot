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
