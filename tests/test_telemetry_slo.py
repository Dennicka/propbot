from __future__ import annotations

import pytest

from app.telemetry import observe_core_latency, observe_ui_latency, reset_for_tests
from app.telemetry.slo import evaluate


@pytest.mark.asyncio
async def test_slo_evaluate_ok() -> None:
    reset_for_tests()
    observe_ui_latency("/api/ui/status", 50.0, status_code=200)
    observe_core_latency("scan", 75.0, error=False)
    result = await evaluate(200.0, 0.5, notify=False)
    assert result.ok
    assert result.breaches == []


@pytest.mark.asyncio
async def test_slo_evaluate_breach(monkeypatch) -> None:
    reset_for_tests()
    messages: list[str] = []

    async def fake_emit(message: str) -> None:
        messages.append(message)

    monkeypatch.setattr("app.telemetry.slo._emit_breach", fake_emit)
    observe_ui_latency("/api/ui/status", 400.0, status_code=500)
    observe_core_latency("hedge", 1200.0, error=True)
    result = await evaluate(100.0, 0.01, notify=True)
    assert not result.ok
    assert messages
    assert any("SLO breach" in entry for entry in messages)
