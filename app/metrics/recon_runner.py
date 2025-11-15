from __future__ import annotations

from app.metrics.core import gauge

recon_venue_state = gauge(
    "propbot_recon_runner_venue_state",
    labels=("venue_id",),
)


__all__ = ["recon_venue_state"]
