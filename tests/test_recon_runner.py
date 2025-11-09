from __future__ import annotations

import pytest

from app.services import runtime
from app.services.recon_runner import ReconRunner


@pytest.mark.asyncio
async def test_recon_runner_invokes_cycle(monkeypatch):
    summary = {"diffs": [{"kind": "position"}], "has_warn": False, "has_crit": False, "state": "OK"}
    statuses: list[dict[str, object]] = []

    async def fake_cycle(*, thresholds=None, enable_hold=None):
        assert enable_hold is True
        runtime.update_reconciliation_status(
            diffs=summary["diffs"],
            metadata={
                "state": summary["state"],
                "has_warn": summary["has_warn"],
                "has_crit": summary["has_crit"],
            },
            desync_detected=bool(summary["diffs"]),
        )
        return summary

    monkeypatch.setattr(runtime, "update_reconciliation_status", lambda **kw: statuses.append(kw) or kw)
    monkeypatch.setattr("app.services.recon_runner.run_recon_cycle", fake_cycle)

    runner = ReconRunner(interval=0.01)
    runner.auto_hold_enabled = True

    result = await runner.run_once()

    assert result["diffs"] == summary["diffs"]
    assert result["signal_hold"] is True
    assert result["auto_hold"] is True
    assert statuses  # status updated at least once


@pytest.mark.asyncio
async def test_recon_runner_handles_exceptions(monkeypatch):
    statuses: list[dict[str, object]] = []

    async def failing_cycle(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime, "update_reconciliation_status", lambda **kw: statuses.append(kw) or kw)
    monkeypatch.setattr("app.services.recon_runner.run_recon_cycle", failing_cycle)

    runner = ReconRunner(interval=0.01)

    with pytest.raises(RuntimeError):
        await runner.run_once()

    assert statuses
    assert statuses[-1]["metadata"]["error"] == "boom"
