from __future__ import annotations

from dataclasses import replace

import pytest

from app.alerts.pipeline import RECON_ISSUES_DETECTED
from app.alerts.recon import emit_recon_alerts
from app.alerts.registry import REGISTRY
from app.recon.models import ReconIssue, ReconSnapshot


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    REGISTRY.clear()


def _make_snapshot(*, issues: tuple[ReconIssue, ...] = tuple()) -> ReconSnapshot:
    return ReconSnapshot(
        venue_id="binance",
        balances_internal=(),
        balances_external=(),
        positions_internal=(),
        positions_external=(),
        open_orders_internal=(),
        open_orders_external=(),
        issues=issues,
    )


def _make_issue(**overrides: object) -> ReconIssue:
    base = ReconIssue(
        severity="warning",
        kind="balance_mismatch",
        venue_id="binance",
        symbol="BTCUSDT",
        asset="USDT",
        message="Mismatch detected",
        internal_value="10",
        external_value="5",
    )
    return replace(base, **overrides)


def test_emit_recon_alerts_no_issues_does_not_emit_error_alerts() -> None:
    snapshot = _make_snapshot(issues=tuple())

    emit_recon_alerts(snapshot)

    assert REGISTRY.last() == []


def test_emit_recon_alerts_with_issue_emits_recon_alert() -> None:
    issue = _make_issue(severity="error", message="Balance mismatch")
    snapshot = _make_snapshot(issues=(issue,))

    emit_recon_alerts(snapshot)

    records = REGISTRY.last()
    assert records
    record = records[-1]
    assert record.source == "recon"
    assert record.level == "ERROR"
    details = dict(record.details)
    assert details.get("event_type") == RECON_ISSUES_DETECTED
    context = details.get("context", {})
    assert context.get("venue_id") == snapshot.venue_id
    issues = context.get("issues", [])
    assert isinstance(issues, list)
    assert issues
    issue_payload = issues[0]
    assert issue_payload.get("kind") == issue.kind
    assert issue_payload.get("message") == issue.message


@pytest.mark.asyncio
async def test_recon_service_emits_alerts_when_snapshot_has_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StubSource:
        async def load_balances(self, venue_id: str):  # pragma: no cover - simple stub
            return []

        async def load_positions(self, venue_id: str):  # pragma: no cover - simple stub
            return []

        async def load_open_orders(self, venue_id: str):  # pragma: no cover - simple stub
            return []

    issue = _make_issue()
    snapshot = _make_snapshot(issues=(issue,))
    monkeypatch.setattr("app.recon.service.build_recon_snapshot", lambda **_: snapshot)

    from app.recon.service import ReconService

    service = ReconService(internal_source=_StubSource(), external_source=_StubSource())

    result = await service.run_for_venue("binance")

    assert result is snapshot
    records = REGISTRY.last()
    assert records
    assert records[-1].source == "recon"
