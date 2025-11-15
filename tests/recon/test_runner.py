from __future__ import annotations

import pytest

from app.recon.models import ReconIssue, ReconIssueSeverity, ReconSnapshot
from app.recon.runner import ReconRunner, ReconRunnerConfig


def _make_snapshot(venue_id: str, issues: list[ReconIssue]) -> ReconSnapshot:
    return ReconSnapshot(
        venue_id=venue_id,
        balances_internal=(),
        balances_external=(),
        positions_internal=(),
        positions_external=(),
        open_orders_internal=(),
        open_orders_external=(),
        issues=tuple(issues),
    )


def _make_issue(venue_id: str, severity: ReconIssueSeverity) -> ReconIssue:
    return ReconIssue(
        severity=severity,
        kind="order_mismatch",
        venue_id=venue_id,
        symbol=None,
        asset=None,
        message="test issue",
    )


class _StubReconService:
    def __init__(self, snapshot: ReconSnapshot) -> None:
        self._snapshot = snapshot

    async def run_for_venue(self, venue_id: str) -> ReconSnapshot:
        return self._snapshot


class _FailingReconService:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def run_for_venue(self, venue_id: str) -> ReconSnapshot:  # pragma: no cover - interface
        raise self._exc


@pytest.mark.asyncio
async def test_runner_marks_failed_when_errors_above_threshold() -> None:
    venue = "test-venue"
    issues = [_make_issue(venue, "error"), _make_issue(venue, "error")]
    service = _StubReconService(_make_snapshot(venue, issues))
    runner = ReconRunner(
        ReconRunnerConfig(venues=[venue], hard_error_threshold=1, soft_warning_threshold=0),
        service=service,
    )

    await runner.run_once()

    status = runner.get_status_for_venue(venue)
    assert status.state == "failed"
    assert status.last_errors == 2
    assert status.last_warnings == 0


@pytest.mark.asyncio
async def test_runner_marks_degraded_when_only_warnings() -> None:
    venue = "warn-venue"
    issues = [_make_issue(venue, "warning"), _make_issue(venue, "warning")]
    service = _StubReconService(_make_snapshot(venue, issues))
    runner = ReconRunner(
        ReconRunnerConfig(venues=[venue], hard_error_threshold=2, soft_warning_threshold=0),
        service=service,
    )

    await runner.run_once()

    status = runner.get_status_for_venue(venue)
    assert status.state == "degraded"
    assert status.last_errors == 0
    assert status.last_warnings == 2


@pytest.mark.asyncio
async def test_runner_marks_ok_when_no_issues() -> None:
    venue = "ok-venue"
    service = _StubReconService(_make_snapshot(venue, []))
    runner = ReconRunner(
        ReconRunnerConfig(venues=[venue], hard_error_threshold=1, soft_warning_threshold=0),
        service=service,
    )

    await runner.run_once()

    status = runner.get_status_for_venue(venue)
    assert status.state == "ok"
    assert status.last_errors == 0
    assert status.last_warnings == 0
    assert status.last_issues_count == 0


@pytest.mark.asyncio
async def test_runner_handles_exception_as_failed() -> None:
    venue = "exception-venue"
    service = _FailingReconService(RuntimeError("boom"))
    runner = ReconRunner(
        ReconRunnerConfig(venues=[venue], hard_error_threshold=1, soft_warning_threshold=0),
        service=service,
    )

    await runner.run_once()

    status = runner.get_status_for_venue(venue)
    assert status.state == "failed"
    assert status.last_errors == 1
    assert status.last_error_message == "boom"
