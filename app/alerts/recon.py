"""Stubs for emitting reconciliation alerts."""

from __future__ import annotations

import logging

from app.recon.models import ReconSnapshot

logger = logging.getLogger(__name__)


def emit_recon_alerts(snapshot: ReconSnapshot) -> None:
    """Emit alerts for recon issues (stub v1)."""

    for issue in snapshot.issues:
        if issue.severity == "error":
            logger.error(
                "recon.issue",
                extra={"kind": issue.kind, "venue": issue.venue_id, "symbol": issue.symbol},
            )
        elif issue.severity == "warning":
            logger.warning(
                "recon.issue",
                extra={"kind": issue.kind, "venue": issue.venue_id, "symbol": issue.symbol},
            )
        else:
            logger.info(
                "recon.issue",
                extra={"kind": issue.kind, "venue": issue.venue_id, "symbol": issue.symbol},
            )
        # NOTE: integrate with real alerts pipeline in future iterations.


__all__ = ["emit_recon_alerts"]
