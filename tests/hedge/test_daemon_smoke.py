from __future__ import annotations

from decimal import Decimal

import pytest

from app.hedge.daemon import AutoHedgeDaemon
from app.hedge.policy import Exposure, HedgePolicy, Quote
import app.metrics.core as metrics_core


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def time(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class StaticPositionProvider:
    def __init__(self, exposure: Exposure) -> None:
        self.exposure = exposure
        self.calls = 0

    def get_exposure_usd(self, symbols: list[str] | None) -> dict[str, Exposure]:
        self.calls += 1
        return {self.exposure.symbol: self.exposure}


class StaticQuoteProvider:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def get_quotes(self, symbol: str) -> dict[str, Quote]:
        now_ms = int(self.clock.time() * 1000)
        return {
            "binance": Quote(
                venue="binance",
                symbol=symbol,
                bid=Decimal("20000"),
                ask=Decimal("20005"),
                ts_ms=now_ms,
            )
        }


class RecordingRouter:
    def __init__(self) -> None:
        self.legs: list = []

    def submit_hedge_leg(self, leg):
        self.legs.append(leg)
        return {"ok": True, "reason": "ok"}


def _configure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FF_AUTO_HEDGE", "1")
    monkeypatch.setenv("HEDGE_SYMBOLS", "BTCUSDT")
    monkeypatch.setenv("HEDGE_MIN_ABS_DELTA_USD", "50")
    monkeypatch.setenv("HEDGE_DEADBAND_USD", "25")
    monkeypatch.setenv("HEDGE_STEP_USD", "250")
    monkeypatch.setenv("HEDGE_MAX_NOTIONAL_USD", "5000")
    monkeypatch.setenv("HEDGE_MAX_SLIPPAGE_BPS", "5")
    monkeypatch.setenv("HEDGE_QUOTE_TTL_MS", "300")
    monkeypatch.setenv("HEDGE_VENUE_PREFS", '{"binance":1.0}')
    monkeypatch.setenv("HEDGE_COOLDOWN_SEC", "5")


def test_smoke_single_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    clock = FakeClock(start=0.0)
    policy = HedgePolicy(now_ms=lambda: int(clock.time() * 1000))
    exposure = Exposure(symbol="BTCUSDT", usd=Decimal("520"))
    positions = StaticPositionProvider(exposure)
    quotes = StaticQuoteProvider(clock)
    router = RecordingRouter()
    daemon = AutoHedgeDaemon(
        policy=policy,
        pos_provider=positions,
        quote_provider=quotes,
        router=router,
        clock=clock,
    )

    first = daemon.tick()
    assert first["status"] == "ok"
    assert len(router.legs) == 1
    assert "BTCUSDT" in daemon._cooldowns

    second = daemon.tick()
    assert second["status"] == "ok"
    assert len(router.legs) == 1

    metrics_text = metrics_core.get_registry().to_text()
    assert "propbot_hedge_submit_total" in metrics_text
    assert 'result="ok"' in metrics_text
    assert "propbot_hedge_blocked_total" in metrics_text
