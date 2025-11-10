from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.recon.core import ReconSnapshot
from app.recon.daemon import DaemonConfig, ReconDaemon


@pytest.mark.asyncio
async def test_recon_triggers_auto_hold_on_critical(monkeypatch: pytest.MonkeyPatch) -> None:
    safety = _build_safety()
    state = SimpleNamespace(safety=safety, control=SimpleNamespace(safe_mode=True))
    monkeypatch.setattr("app.recon.daemon.runtime.get_state", lambda: state)
    async def fake_fetch(_state):
        return []

    monkeypatch.setattr("app.recon.daemon._fetch_remote_balances", fake_fetch)

    def engage(reason: str, *, source: str) -> bool:
        safety.hold_active = True
        safety.hold_reason = reason
        return True

    monkeypatch.setattr("app.recon.daemon.runtime.engage_safety_hold", engage)
    monkeypatch.setattr("app.recon.daemon.runtime.autopilot_apply_resume", lambda **kwargs: None)
    monkeypatch.setattr("app.recon.daemon.runtime.update_reconciliation_status", lambda **kwargs: kwargs)
    monkeypatch.setattr("app.recon.daemon.log_operator_action", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.recon.daemon.get_golden_logger", lambda: SimpleNamespace(enabled=False))

    critical_snapshot = ReconSnapshot(
        venue="binance-um",
        asset="USDT",
        symbol="ETHUSDT",
        side=None,
        exch_position=Decimal("0"),
        local_position=Decimal("0"),
        exch_balance=None,
        local_balance=None,
        diff_abs=Decimal("100"),
        status="CRITICAL",
        reason="position_mismatch",
        ts=123.0,
    )

    monkeypatch.setattr("app.recon.daemon.reconcile_once", lambda ctx: [critical_snapshot])

    daemon = ReconDaemon(DaemonConfig(enabled=True, clear_after_ok_runs=1))
    snapshots = await daemon.run_once()

    assert snapshots == [critical_snapshot]
    assert safety.hold_active is True
    assert safety.hold_reason == "RECON_DIVERGENCE"
    assert daemon.auto_hold_active is True


@pytest.mark.asyncio
async def test_recon_clears_auto_hold_after_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    safety = _build_safety()
    state = SimpleNamespace(safety=safety, control=SimpleNamespace(safe_mode=True))
    monkeypatch.setattr("app.recon.daemon.runtime.get_state", lambda: state)
    async def fake_fetch(_state):
        return []

    monkeypatch.setattr("app.recon.daemon._fetch_remote_balances", fake_fetch)
    monkeypatch.setattr("app.recon.daemon.runtime.update_reconciliation_status", lambda **kwargs: kwargs)
    monkeypatch.setattr("app.recon.daemon.log_operator_action", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.recon.daemon.get_golden_logger", lambda: SimpleNamespace(enabled=False))

    def engage(reason: str, *, source: str) -> bool:
        safety.hold_active = True
        safety.hold_reason = reason
        return True

    resume_calls: list[dict[str, Any]] = []

    def resume(*, safe_mode: bool) -> None:
        resume_calls.append({"safe_mode": safe_mode})
        safety.hold_active = False
        safety.hold_reason = None

    monkeypatch.setattr("app.recon.daemon.runtime.engage_safety_hold", engage)
    monkeypatch.setattr("app.recon.daemon.runtime.autopilot_apply_resume", resume)

    critical_snapshot = ReconSnapshot(
        venue="binance-um",
        asset="USDT",
        symbol="ETHUSDT",
        side=None,
        exch_position=Decimal("0"),
        local_position=Decimal("0"),
        exch_balance=None,
        local_balance=None,
        diff_abs=Decimal("100"),
        status="CRITICAL",
        reason="position_mismatch",
        ts=123.0,
    )
    ok_snapshot = ReconSnapshot(
        venue="binance-um",
        asset="USDT",
        symbol="ETHUSDT",
        side=None,
        exch_position=Decimal("0"),
        local_position=Decimal("0"),
        exch_balance=None,
        local_balance=None,
        diff_abs=Decimal("0"),
        status="OK",
        reason="position_ok",
        ts=124.0,
    )

    snapshots_sequence = [[critical_snapshot], [ok_snapshot], [ok_snapshot]]

    def fake_reconcile(ctx):
        return snapshots_sequence.pop(0)

    monkeypatch.setattr("app.recon.daemon.reconcile_once", fake_reconcile)

    daemon = ReconDaemon(DaemonConfig(enabled=True, clear_after_ok_runs=2))

    await daemon.run_once()  # engage hold
    assert daemon.auto_hold_active is True
    assert safety.hold_active is True

    await daemon.run_once()
    assert daemon.auto_hold_active is True
    assert safety.hold_active is True

    await daemon.run_once()

    assert daemon.auto_hold_active is False
    assert safety.hold_active is False
    assert resume_calls


def _build_safety() -> SimpleNamespace:
    safety = SimpleNamespace(
        risk_snapshot={},
        hold_active=False,
        hold_reason=None,
        reconciliation_snapshot={},
    )

    def status_payload() -> dict[str, Any]:
        return {
            "hold_active": safety.hold_active,
            "hold_reason": safety.hold_reason,
            "reconciliation": safety.reconciliation_snapshot,
            "runaway_guard": {},
        }

    safety.status_payload = status_payload  # type: ignore[attr-defined]
    return safety
