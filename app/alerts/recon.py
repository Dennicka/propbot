"""Emit structured alerts for reconciliation issues."""

from __future__ import annotations

import logging

from app.recon.models import ReconIssue, ReconSnapshot

logger = logging.getLogger(__name__)


def emit_recon_alerts(snapshot: ReconSnapshot) -> None:
    """Emit alerts for recon issues (stub v1).

    Пока просто логируем; дальше можно завести Telegram/ops-пайплайн.
    """

    if not snapshot.issues:
        return

    for issue in snapshot.issues:
        if issue.severity == "error":
            logger.error(
                "Recon error: %s",
                issue.message,
                extra=_build_extra(issue, include_values=True),
            )
        elif issue.severity == "warning":
            logger.warning(
                "Recon warning: %s",
                issue.message,
                extra=_build_extra(issue, include_values=False),
            )
        else:
            logger.info(
                "Recon info: %s",
                issue.message,
                extra=_build_extra(issue, include_values=False),
            )


def _build_extra(issue: ReconIssue, *, include_values: bool) -> dict[str, object | None]:
    payload: dict[str, object | None] = {
        "venue_id": issue.venue_id,
        "symbol": issue.symbol,
        "asset": issue.asset,
        "kind": issue.kind,
    }
    if include_values:
        payload["internal_value"] = issue.internal_value
        payload["external_value"] = issue.external_value
    return payload


__all__ = ["emit_recon_alerts"]
