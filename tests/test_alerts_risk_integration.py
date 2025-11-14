from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict

import pytest

from app.alerts.pipeline import RISK_LIMIT_BREACHED
from app.risk.limits import RiskConfig, RiskGovernor


class DummyPipeline:
    def __init__(self) -> None:
        self.events: list[Dict[str, Any]] = []

    def notify_event(self, **payload: Any) -> None:
        copied = dict(payload)
        context = payload.get("context")
        if isinstance(context, dict):
            copied["context"] = dict(context)
        self.events.append(copied)


def test_risk_governor_emits_alert_on_venue_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXEC_PROFILE", "paper")
    cfg = RiskConfig(cap_per_venue={"binance": Decimal("100")})
    pipeline = DummyPipeline()
    governor = RiskGovernor(cfg, alerts=pipeline)

    ok, reason = governor.allow_order(
        venue="binance",
        symbol="BTCUSDT",
        strategy="alpha",
        price=Decimal("10"),
        qty=Decimal("20"),
        now_s=0,
    )

    assert not ok
    assert reason == "venue_cap"
    assert pipeline.events

    event = pipeline.events[-1]
    assert event["event_type"] == RISK_LIMIT_BREACHED
    context = event.get("context", {})
    assert context.get("limit_type") == "venue_cap"
    assert context.get("venue") == "binance"
    assert context.get("strategy") == "alpha"
    assert context.get("profile") == "paper"
    assert "limit" in context
