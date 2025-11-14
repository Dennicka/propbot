from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

import pytest

from app.alerts.pipeline import PNL_CAP_BREACHED
from app.risk.pnl_caps import CapsPolicy, FillEvent, PnLAggregator, PnLCapsGuard


class DummyPipeline:
    def __init__(self) -> None:
        self.events: list[Dict[str, Any]] = []

    def notify_event(self, **payload: Any) -> None:
        copied = dict(payload)
        context = payload.get("context")
        if isinstance(context, dict):
            copied["context"] = dict(context)
        self.events.append(copied)


class FakeClock:
    def __init__(self, start: float) -> None:
        self._now = float(start)

    def time(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)


def test_pnl_guard_emits_alert_on_global_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FF_DAILY_LOSS_CAP", "1")
    monkeypatch.setenv("EXEC_PROFILE", "testnet")
    policy = CapsPolicy()
    policy.cap_global = Decimal("100")
    policy.dd_global = Decimal("0")

    pipeline = DummyPipeline()
    clock = FakeClock(1_670_000_000.0)
    agg = PnLAggregator(policy.tz)
    guard = PnLCapsGuard(policy, agg, clock=clock, alerts=pipeline)

    agg.on_fill(
        FillEvent(
            t=clock.time(),
            strategy="alpha",
            symbol="BTCUSDT",
            realized_pnl_usd=Decimal("-150"),
        )
    )

    should_block, reason = guard.should_block("alpha")

    assert should_block
    assert "daily-loss-cap" in reason
    assert pipeline.events

    event = pipeline.events[-1]
    assert event["event_type"] == PNL_CAP_BREACHED
    context = event.get("context", {})
    assert context.get("scope") == "global"
    assert context.get("profile") == "testnet"
    assert "current_pnl" in context
    assert "pnl_cap" in context
