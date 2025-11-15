from __future__ import annotations

from typing import Optional

from app.recon.runner import ReconRunner, ReconRunnerConfig

_runner: Optional[ReconRunner] = None


def set_recon_runner(runner: ReconRunner) -> None:
    global _runner
    _runner = runner


def get_recon_runner() -> ReconRunner:
    global _runner
    if _runner is None:
        _runner = ReconRunner(config=ReconRunnerConfig(venues=[]))
    return _runner


__all__ = ["set_recon_runner", "get_recon_runner"]
