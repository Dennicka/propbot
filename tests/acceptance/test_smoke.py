from __future__ import annotations

import os
import threading
import time
from pathlib import Path
import subprocess

import httpx
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = ROOT / "scripts" / "smoke.sh"


@pytest.mark.skipif(not SMOKE_SCRIPT.exists(), reason="smoke script missing")
def test_smoke_script(tmp_path, unused_tcp_port: int) -> None:
    os.environ.setdefault("AUTH_ENABLED", "false")
    os.environ.setdefault("API_TOKEN", "smoke-token")
    port = unused_tcp_port
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    try:
        deadline = time.time() + 10.0
        with httpx.Client() as client:
            while time.time() < deadline:
                try:
                    response = client.get(f"http://127.0.0.1:{port}/healthz", timeout=1.0)
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                pytest.fail("smoke server did not start")

        env = os.environ.copy()
        env.update({
            "SMOKE_HOST": f"http://127.0.0.1:{port}",
            "SMOKE_TIMEOUT": "2",
            "SMOKE_TOKEN": env.get("API_TOKEN", ""),
        })
        result = subprocess.run(
            [str(SMOKE_SCRIPT)],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        output = result.stdout
        assert "✅" in output
        assert result.returncode == 0
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        if thread.is_alive():
            pytest.fail("uvicorn server did not stop")
