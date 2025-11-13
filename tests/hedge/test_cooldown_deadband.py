from __future__ import annotations

from decimal import Decimal

import pytest

from app.hedge.daemon import AutoHedgeDaemon
from app.hedge.policy import Exposure, HedgePolicy, Quote


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def time(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class SequencePositionProvider:
    def __init__(self, exposures: list[dict[str, Exposure]]) -> None:
        self._exposures = exposures
        self._index = 0

    def get_exposure_usd(self, symbols: list[str] | None) -> dict[str, Exposure]:
        current = self._exposures[min(self._index, len(self._exposures) - 1)]
        self._index += 1
        return current


class FreshQuoteProvider:
    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    def get_quotes(self, symbol: str) -> dict[str, Quote]:
        ts_ms = int(self._clock.time() * 1000)
        return {
            "binance": Quote(
                venue="binance",
                symbol=symbol,
                bid=Decimal("20000"),
                ask=Decimal("20005"),
                ts_ms=ts_ms,
            )
        }


class RecordingRouter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def submit_hedge_leg(self, leg):
        self.calls.append((leg.symbol, leg.side))
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


def test_deadband_and_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    clock = FakeClock(start=0.0)
    policy = HedgePolicy(now_ms=lambda: int(clock.time() * 1000))
    exposures = [
        {"BTCUSDT": Exposure(symbol="BTCUSDT", usd=Decimal("520"))},
        {"BTCUSDT": Exposure(symbol="BTCUSDT", usd=Decimal("30"))},
        {"BTCUSDT": Exposure(symbol="BTCUSDT", usd=Decimal("-20"))},
        {"BTCUSDT": Exposure(symbol="BTCUSDT", usd=Decimal("400"))},
    ]
    positions = SequencePositionProvider(exposures)
    quotes = FreshQuoteProvider(clock)
    router = RecordingRouter()
    daemon = AutoHedgeDaemon(
        policy=policy,
        pos_provider=positions,
        quote_provider=quotes,
        router=router,
        clock=clock,
    )

    first = daemon.tick()
    assert first["processed"][0]["status"] == "submitted"
    assert len(router.calls) == 1

    second = daemon.tick()
    assert second["processed"][0]["status"] == "cooldown"
    third = daemon.tick()
    assert third["processed"][0]["status"] == "cooldown"
    assert len(router.calls) == 1

    clock.advance(5.0)
    final = daemon.tick()
    assert final["processed"][0]["status"] == "submitted"
    assert len(router.calls) == 2
