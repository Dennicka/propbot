from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import runtime
from app.services.recon_runner import ReconRunner


@pytest.mark.asyncio
async def test_recon_runner_invokes_cycle(monkeypatch):
    snapshots = [SimpleNamespace(status="WARN")]

    class StubDaemon:
        auto_hold_active = True

        def __init__(self, *_args, **_kwargs) -> None:
            self.calls = 0

        async def run_once(self) -> list[object]:
            self.calls += 1
            return snapshots

    stub = StubDaemon()
    monkeypatch.setattr("app.services.recon_runner.ReconDaemon", lambda *_a, **_k: stub)

    runner = ReconRunner(interval=0.01)
    result = await runner.run_once()

    assert result["snapshots"] == snapshots
    assert result["worst_state"] == "WARN"
    assert result["auto_hold"] is True
    assert stub.calls == 1


@pytest.mark.asyncio
async def test_recon_runner_handles_exceptions(monkeypatch):
    async def failing_run_once(self):
        raise RuntimeError("boom")

    class StubDaemon:
        auto_hold_active = False

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        run_once = failing_run_once

    monkeypatch.setattr("app.services.recon_runner.ReconDaemon", StubDaemon)
    monkeypatch.setattr(
        runtime,
        "update_reconciliation_status",
        lambda **kw: kw,
    )

    runner = ReconRunner(interval=0.01)

    with pytest.raises(RuntimeError):
        await runner.run_once()
