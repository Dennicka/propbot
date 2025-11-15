from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.recon.models import ReconVenueStatus
from app.recon.runner import ReconRunner, ReconRunnerConfig
from app.recon.runner_registry import set_recon_runner


def test_recon_runner_status_endpoint(client: TestClient) -> None:
    now = datetime.now(timezone.utc)
    statuses = [
        ReconVenueStatus(
            venue_id="alpha",
            state="ok",
            last_run_ts=now,
            last_errors=0,
            last_warnings=0,
            last_issues_count=0,
            last_error_message=None,
        ),
        ReconVenueStatus(
            venue_id="beta",
            state="failed",
            last_run_ts=now,
            last_errors=3,
            last_warnings=1,
            last_issues_count=4,
            last_error_message="boom",
        ),
    ]

    runner = ReconRunner(ReconRunnerConfig(venues=[]))
    setattr(runner, "_statuses", {status.venue_id: status for status in statuses})
    set_recon_runner(runner)
    try:
        response = client.get("/api/ui/recon/runner-status")
    finally:
        set_recon_runner(ReconRunner(ReconRunnerConfig(venues=[])))

    assert response.status_code == 200
    payload = response.json()
    assert "venues" in payload
    assert isinstance(payload["venues"], list)
    assert payload["venues"][0]["venue_id"] == "alpha"
