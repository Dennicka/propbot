from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.recon.daemon import DaemonConfig, ReconDaemon
from app.recon.core import ReconIssue, ReconResult


@pytest.mark.asyncio
async def test_recon_sets_hold_on_critical_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    safety = SimpleNamespace(hold_active=False, hold_reason=None)
    state = SimpleNamespace(derivatives=None, safety=safety)
    monkeypatch.setattr("app.recon.daemon.runtime.get_state", lambda: state)

    issues = [
        ReconIssue(
            kind="POSITION",
            venue="binance-um",
            symbol="ETHUSDT",
            severity="CRITICAL",
            code="POSITION_MISMATCH",
            details="mismatch",
        )
    ]
    result = ReconResult(ts=123.0, issues=issues)

    async def fake_context(_self, _state):
        return SimpleNamespace()

    monkeypatch.setattr("app.recon.daemon.ReconDaemon._build_context", fake_context)
    monkeypatch.setattr("app.recon.daemon.reconcile_once", lambda ctx: result)

    holds: dict[str, str] = {}

    def engage(reason: str, *, source: str) -> bool:
        holds["reason"] = reason
        return True

    metadata_calls: list[dict[str, object]] = []

    def update_status(**kwargs) -> None:
        metadata_calls.append(kwargs.get("metadata", {}))

    monkeypatch.setattr("app.recon.daemon.runtime.engage_safety_hold", engage)
    monkeypatch.setattr("app.recon.daemon.runtime.update_reconciliation_status", update_status)

    daemon = ReconDaemon(
        DaemonConfig(
            enabled=True,
            interval_sec=1.0,
            epsilon_position=Decimal("0.0001"),
            epsilon_balance=Decimal("0.5"),
            epsilon_notional=Decimal("1.0"),
            auto_hold_on_critical=True,
        )
    )

    outcome = await daemon.run_once()

    assert outcome is result
    assert holds["reason"] == "RECON_CRITICAL::POSITION_MISMATCH"
    assert metadata_calls
    assert metadata_calls[-1].get("status") == "CRITICAL"
    assert metadata_calls[-1].get("auto_hold") is True
