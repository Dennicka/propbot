from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.metrics.recon import RECON_LAST_RUN_TS  # type: ignore[attr-defined]
from app.metrics.recon_runner import recon_venue_state
from app.recon.kill_switch import apply_recon_kill_switch
from app.recon.models import (
    ReconRunnerVenueState,
    ReconVenueStatus,
    VenueId,
)
from app.recon.service import ReconService

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ReconRunnerConfig:
    """Configuration for periodic recon runner."""

    venues: list[VenueId]
    hard_error_threshold: int = 1
    soft_warning_threshold: int = 0


class ReconRunner:
    """Periodic recon runner that aggregates status per venue."""

    def __init__(
        self,
        config: ReconRunnerConfig,
        service: ReconService | None = None,
    ) -> None:
        self._config = config
        self._service = service or ReconService()
        self._statuses: dict[VenueId, ReconVenueStatus] = {}

    async def run_once(self) -> None:
        """Run recon once for all configured venues and update internal statuses."""

        now = datetime.now(timezone.utc)
        for venue_id in self._config.venues:
            try:
                snapshot = await self._service.run_for_venue(venue_id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("recon.runner.iteration_failed", extra={"venue_id": venue_id})
                self._statuses[venue_id] = ReconVenueStatus(
                    venue_id=venue_id,
                    state="failed",
                    last_run_ts=now,
                    last_errors=1,
                    last_warnings=0,
                    last_issues_count=0,
                    last_error_message=str(exc),
                )
                continue

            errors = sum(1 for issue in snapshot.issues if issue.severity == "error")
            warnings = sum(1 for issue in snapshot.issues if issue.severity == "warning")
            total = len(snapshot.issues)

            if errors >= self._config.hard_error_threshold:
                state: ReconRunnerVenueState = "failed"
            elif warnings > self._config.soft_warning_threshold:
                state = "degraded"
            else:
                state = "ok"

            self._statuses[venue_id] = ReconVenueStatus(
                venue_id=venue_id,
                state=state,
                last_run_ts=now,
                last_errors=errors,
                last_warnings=warnings,
                last_issues_count=total,
                last_error_message=None,
            )

        for status in self._statuses.values():
            state_value = {
                "unknown": 0,
                "ok": 1,
                "degraded": 2,
                "failed": 3,
            }[status.state]
            recon_venue_state.labels(venue_id=status.venue_id).set(state_value)

        if self._statuses:
            RECON_LAST_RUN_TS.set(now.timestamp())

        apply_recon_kill_switch(self.get_all_statuses())

    def get_status_for_venue(self, venue_id: VenueId) -> ReconVenueStatus:
        """Return last known status for a venue (or 'unknown' stub)."""

        status = self._statuses.get(venue_id)
        if status is not None:
            return status
        return ReconVenueStatus(
            venue_id=venue_id,
            state="unknown",
            last_run_ts=None,
            last_errors=0,
            last_warnings=0,
            last_issues_count=0,
            last_error_message=None,
        )

    def get_all_statuses(self) -> list[ReconVenueStatus]:
        """Return statuses for all venues runner knows about."""

        return list(self._statuses.values())


__all__ = ["ReconRunner", "ReconRunnerConfig"]
