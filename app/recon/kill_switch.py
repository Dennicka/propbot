from __future__ import annotations

import logging
from typing import Iterable

from app.recon.models import ReconVenueStatus

logger = logging.getLogger(__name__)


def apply_recon_kill_switch(statuses: Iterable[ReconVenueStatus]) -> None:
    """Apply kill-switch-like reaction based on recon statuses."""

    for status in statuses:
        if status.state == "failed":
            logger.error(
                "Recon failed for venue %s: errors=%s warnings=%s",
                status.venue_id,
                status.last_errors,
                status.last_warnings,
            )
        elif status.state == "degraded":
            logger.warning(
                "Recon degraded for venue %s: warnings=%s",
                status.venue_id,
                status.last_warnings,
            )
        else:
            logger.debug(
                "Recon status for venue %s: state=%s",
                status.venue_id,
                status.state,
            )


__all__ = ["apply_recon_kill_switch"]
