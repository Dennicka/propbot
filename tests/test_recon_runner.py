from __future__ import annotations

import pytest

from app.services import runtime
from app.services.recon_runner import ReconRunner


class DummyReconciler:
    def __init__(self, diffs):
        self._diffs = diffs

    def diff(self):
        return list(self._diffs)


@pytest.mark.asyncio
async def test_recon_runner_triggers_auto_hold(monkeypatch):
    diffs = [
        {
            "venue": "binance-um",
            "symbol": "BTCUSDT",
            "exch_qty": 1.5,
            "ledger_qty": 0.5,
            "delta": 1.0,
            "notional_usd": 50_000.0,
        }
    ]
    statuses: list[dict[str, object]] = []
    alerts: list[tuple[str, str, dict | None]] = []
    holds: list[tuple[str, str]] = []
    audits: list[tuple[str, str, str, dict | None]] = []

    def fake_update(**kwargs):
        statuses.append(kwargs)
        return kwargs

    def fake_alert(kind: str, text: str, extra: dict | None = None):
        alerts.append((kind, text, extra))

    def fake_hold(reason: str, *, source: str):
        holds.append((reason, source))
        return True

    def fake_audit(operator: str, role: str, action: str, details=None):
        audits.append((operator, role, action, details))

    monkeypatch.setattr(runtime, "update_reconciliation_status", fake_update)
    monkeypatch.setattr(runtime, "send_notifier_alert", fake_alert)
    monkeypatch.setattr(runtime, "engage_safety_hold", fake_hold)
    monkeypatch.setattr("app.services.recon_runner.log_operator_action", fake_audit)

    runner = ReconRunner(reconciler=DummyReconciler(diffs), interval=0.01)
    runner.auto_hold_enabled = True

    result = await runner.run_once()

    assert result["diffs"] == diffs
    assert holds == [("auto_hold:reconciliation", "reconciliation_runner")]
    assert any(entry[2] == "AUTO_HOLD_RECON" for entry in audits)
    kinds = [entry[0] for entry in alerts]
    assert "recon_diff" in kinds
    assert "auto_hold_recon" in kinds
    assert statuses and statuses[-1]["desync_detected"] is True


@pytest.mark.asyncio
async def test_recon_runner_no_hold_for_small_diffs(monkeypatch):
    statuses: list[dict[str, object]] = []
    alerts: list[tuple[str, str, dict | None]] = []
    holds: list[tuple[str, str]] = []

    monkeypatch.setattr(runtime, "update_reconciliation_status", lambda **kw: statuses.append(kw) or kw)
    monkeypatch.setattr(runtime, "send_notifier_alert", lambda *args, **kw: alerts.append((args[0], args[1], kw.get("extra"))))
    monkeypatch.setattr(runtime, "engage_safety_hold", lambda *args, **kw: holds.append((args, kw)))

    runner = ReconRunner(reconciler=DummyReconciler([]), interval=0.01)
    runner.auto_hold_enabled = True

    result = await runner.run_once()

    assert result["diffs"] == []
    assert not holds
    assert alerts == []
    assert statuses and statuses[-1]["desync_detected"] is False
