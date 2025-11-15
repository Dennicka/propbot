from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.recon.kill_switch import apply_recon_kill_switch
from app.recon.models import ReconVenueStatus


def test_apply_recon_kill_switch_logs_states(caplog) -> None:
    now = datetime.now(timezone.utc)
    statuses = [
        ReconVenueStatus(
            venue_id="failed-venue",
            state="failed",
            last_run_ts=now,
            last_errors=2,
            last_warnings=0,
            last_issues_count=2,
            last_error_message="boom",
        ),
        ReconVenueStatus(
            venue_id="degraded-venue",
            state="degraded",
            last_run_ts=now,
            last_errors=0,
            last_warnings=1,
            last_issues_count=1,
            last_error_message=None,
        ),
        ReconVenueStatus(
            venue_id="ok-venue",
            state="ok",
            last_run_ts=now,
            last_errors=0,
            last_warnings=0,
            last_issues_count=0,
            last_error_message=None,
        ),
    ]

    with caplog.at_level(logging.DEBUG):
        apply_recon_kill_switch(statuses)

    messages = [record.message for record in caplog.records]
    assert any("Recon failed" in message for message in messages)
    assert any("Recon degraded" in message for message in messages)
    assert any("Recon status" in message for message in messages)
