from __future__ import annotations

import pytest

from app.recon.core import ReconIssue, ReconResult
from app.services import runtime
from app.services.recon_runner import ReconRunner


@pytest.mark.asyncio
async def test_recon_runner_invokes_cycle(monkeypatch):
    class StubDaemon:
        def __init__(self, *_args, **_kwargs) -> None:
            self.calls = 0

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def run_once(self):
            self.calls += 1
            return ReconResult(
                ts=1.0,
                issues=[
                    ReconIssue(
                        kind="POSITION",
                        venue="binance-um",
                        symbol="BTCUSDT",
                        severity="WARN",
                        code="POSITION_MISMATCH",
                        details="delta",
                    )
                ],
            )

    stub = StubDaemon()
    monkeypatch.setattr("app.services.recon_runner.ReconDaemon", lambda *_a, **_k: stub)

    runner = ReconRunner(interval=0.01)
    result = await runner.run_once()

    assert isinstance(result, ReconResult)
    assert result.issues
    assert stub.calls == 1


@pytest.mark.asyncio
async def test_recon_runner_handles_exceptions(monkeypatch):
    async def failing_run_once(self):
        raise RuntimeError("boom")

    class StubDaemon:
        def __init__(self, *_args, **_kwargs) -> None:
            return

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

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
