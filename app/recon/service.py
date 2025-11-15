"""High-level reconciliation service comparing internal and external state."""

from __future__ import annotations

import logging

from typing import Any

from app.alerts.recon import emit_recon_alerts
from app.recon.engine import build_recon_snapshot
from app.recon.external_source import ExternalStateSource
from app.recon.internal_source import InternalStateSource
from app.recon.models import ReconSnapshot, VenueId

from .service_legacy import ReconResult, reconcile_once as _legacy_reconcile_once

LOGGER = logging.getLogger(__name__)

# expose reconcile_once for backwards compatibility and monkeypatch support
reconcile_once = _legacy_reconcile_once


def collect_recon_snapshot(ctx: object | None = None) -> ReconResult:
    """Run a reconciliation cycle and return the structured result."""

    try:
        return reconcile_once(ctx)
    except Exception:  # pragma: no cover - defensive
        LOGGER.exception("collect_recon_snapshot.failed")
        raise


class ReconService:
    """High-level reconciliation service between internal and external state."""

    def __init__(
        self,
        internal_source: InternalStateSource | None = None,
        external_source: ExternalStateSource | None = None,
    ) -> None:
        self._internal = internal_source or InternalStateSource()
        self._external = external_source or ExternalStateSource()

    async def run_for_venue(self, venue_id: VenueId) -> ReconSnapshot:
        profile_name: str | None = None
        try:
            from app.services.runtime import get_runtime_profile_snapshot

            snapshot: dict[str, Any] = get_runtime_profile_snapshot()
            profile_name = str(snapshot.get("name")) if snapshot else None
        except Exception:  # pragma: no cover - runtime may not be initialised in tests
            profile_name = None

        LOGGER.info(
            "recon.run_for_venue",
            extra={"venue_id": venue_id, "runtime_profile": profile_name},
        )
        balances_internal = await self._internal.load_balances(venue_id)
        positions_internal = await self._internal.load_positions(venue_id)
        orders_internal = await self._internal.load_open_orders(venue_id)

        balances_external = await self._external.load_balances(venue_id)
        positions_external = await self._external.load_positions(venue_id)
        orders_external = await self._external.load_open_orders(venue_id)

        snapshot = build_recon_snapshot(
            venue_id=venue_id,
            balances_internal=balances_internal,
            balances_external=balances_external,
            positions_internal=positions_internal,
            positions_external=positions_external,
            orders_internal=orders_internal,
            orders_external=orders_external,
        )
        emit_recon_alerts(snapshot)
        return snapshot


__all__ = ["ReconService", "collect_recon_snapshot", "reconcile_once"]
