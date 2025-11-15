from __future__ import annotations

from app.alerts.pipeline import RECON_ISSUES_DETECTED
from app.alerts.recon import emit_recon_alerts
from app.alerts.registry import REGISTRY
from app.recon.models import ReconIssue, ReconSnapshot


def _empty_snapshot(*, issues: list[ReconIssue]) -> ReconSnapshot:
    return ReconSnapshot(
        venue_id="test",
        balances_internal=[],
        balances_external=[],
        positions_internal=[],
        positions_external=[],
        open_orders_internal=[],
        open_orders_external=[],
        issues=issues,
    )


def test_emit_recon_alerts_no_issues() -> None:
    REGISTRY.clear()
    emit_recon_alerts(_empty_snapshot(issues=[]))
    assert not REGISTRY.last()


def test_emit_recon_alerts_records_highest_severity() -> None:
    REGISTRY.clear()
    issues = [
        ReconIssue(
            severity="error",
            kind="balance_mismatch",
            venue_id="test",
            symbol="BTCUSDT",
            asset="BTC",
            message="Balance mismatch",
            internal_value="1.0",
            external_value="0.5",
        ),
        ReconIssue(
            severity="warning",
            kind="order_mismatch",
            venue_id="test",
            symbol="ETHUSDT",
            asset="ETH",
            message="Order pending",
        ),
    ]
    emit_recon_alerts(_empty_snapshot(issues=issues))

    records = REGISTRY.last()
    assert records
    record = records[-1]
    assert record.level == "ERROR"
    assert record.source == "recon"
    details = dict(record.details)
    assert details.get("event_type") == RECON_ISSUES_DETECTED
    context = details.get("context", {})
    assert context.get("issue_count") == len(issues)
