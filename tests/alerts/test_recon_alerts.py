from __future__ import annotations

import logging

import pytest

from app.alerts.recon import emit_recon_alerts
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


def test_emit_recon_alerts_no_issues(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        emit_recon_alerts(_empty_snapshot(issues=[]))
    assert not caplog.records


def test_emit_recon_alerts_logs_severities(caplog: pytest.LogCaptureFixture) -> None:
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
    with caplog.at_level(logging.INFO):
        emit_recon_alerts(_empty_snapshot(issues=issues))

    levels = {record.levelno for record in caplog.records}
    assert logging.ERROR in levels
    assert logging.WARNING in levels
