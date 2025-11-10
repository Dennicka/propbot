"""High-level helpers for collecting reconciliation issues."""

from __future__ import annotations

import logging

from .core import ReconResult, reconcile_once

LOGGER = logging.getLogger(__name__)


def collect_recon_snapshot(ctx: object | None = None) -> ReconResult:
    """Run a reconciliation cycle and return the structured result."""

    try:
        return reconcile_once(ctx)
    except Exception:  # pragma: no cover - defensive
        LOGGER.exception("collect_recon_snapshot.failed")
        raise


__all__ = ["collect_recon_snapshot", "ReconResult", "reconcile_once"]
