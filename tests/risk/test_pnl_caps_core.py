from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.risk.pnl_caps import CapsPolicy, FillEvent, PnLAggregator, PnLCapsGuard


class FakeClock:
    def __init__(self, start: float) -> None:
        self._now = start

    def time(self) -> float:
        return self._now

    def set(self, value: float) -> None:
        self._now = value

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def policy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FF_DAILY_LOSS_CAP", "1")
    monkeypatch.setenv("PNL_TZ", "UTC")
    monkeypatch.setenv("DAILY_LOSS_CAP_USD_GLOBAL", "100")
    monkeypatch.setenv("DAILY_LOSS_CAP_USD_PER_STRAT", '{"xarb-perp": 60}')
    monkeypatch.setenv("INTRADAY_DRAWDOWN_CAP_USD_GLOBAL", "50")
    monkeypatch.setenv("PNL_CAPS_COOLOFF_MIN", "1")


def _make_components(start: float) -> tuple[CapsPolicy, PnLAggregator, PnLCapsGuard, FakeClock]:
    policy = CapsPolicy()
    clock = FakeClock(start)
    aggregator = PnLAggregator(policy.tz)
    guard = PnLCapsGuard(policy, aggregator, clock=clock)
    return policy, aggregator, guard, clock


def _ts(day_offset: int = 0, seconds: float = 0.0) -> float:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
    return base + day_offset * 86400 + seconds


def test_pnl_caps_global_and_strategy(policy_env: None) -> None:
    _, aggregator, guard, clock = _make_components(_ts())

    # Global loss accumulation without triggering cap.
    clock.set(_ts(seconds=10))
    aggregator.on_fill(
        FillEvent(
            t=clock.time(),
            strategy="auto_hedge",
            symbol="BTCUSDT",
            realized_pnl_usd=Decimal("-20"),
        )
    )
    clock.set(_ts(seconds=20))
    aggregator.on_fill(
        FillEvent(
            t=clock.time(),
            strategy="auto_hedge",
            symbol="BTCUSDT",
            realized_pnl_usd=Decimal("-30"),
        )
    )
    blocked, reason = guard.should_block("auto_hedge")
    assert (blocked, reason) == (False, "ok")

    # Exceed global loss cap and enter cooloff.
    clock.set(_ts(seconds=30))
    aggregator.on_fill(
        FillEvent(
            t=clock.time(),
            strategy="auto_hedge",
            symbol="BTCUSDT",
            realized_pnl_usd=Decimal("-60"),
        )
    )
    blocked, reason = guard.should_block("auto_hedge")
    assert blocked is True
    assert reason == "daily-loss-cap-global"
    snap_global = aggregator.snapshot(now=clock.time())["global"]
    assert snap_global.cooloff_until > clock.time()

    # New day resets the cap.
    clock.set(_ts(day_offset=1, seconds=5))
    blocked, reason = guard.should_block("auto_hedge")
    assert (blocked, reason) == (False, "ok")

    # Per-strategy daily cap for xarb-perp.
    clock.set(_ts(day_offset=2, seconds=10))
    aggregator.on_fill(
        FillEvent(
            t=clock.time(),
            strategy="xarb-perp",
            symbol="ETHUSDT",
            realized_pnl_usd=Decimal("-30"),
        )
    )
    clock.set(_ts(day_offset=2, seconds=20))
    aggregator.on_fill(
        FillEvent(
            t=clock.time(),
            strategy="xarb-perp",
            symbol="ETHUSDT",
            realized_pnl_usd=Decimal("-35"),
        )
    )
    blocked, reason = guard.should_block("xarb-perp")
    assert blocked is True
    assert reason == "daily-loss-cap-xarb-perp"
    snap_per = aggregator.snapshot(now=clock.time())["per_strat"]["xarb-perp"]
    assert snap_per.cooloff_until > clock.time()

    # Drawdown cap on a separate day.
    clock.set(_ts(day_offset=3, seconds=5))
    aggregator.on_fill(
        FillEvent(
            t=clock.time(),
            strategy="auto_hedge",
            symbol="BTCUSDT",
            realized_pnl_usd=Decimal("40"),
        )
    )
    clock.set(_ts(day_offset=3, seconds=15))
    aggregator.on_fill(
        FillEvent(
            t=clock.time(),
            strategy="auto_hedge",
            symbol="BTCUSDT",
            realized_pnl_usd=Decimal("-70"),
        )
    )
    blocked, reason = guard.should_block("auto_hedge")
    assert blocked is True
    assert reason == "drawdown-cap-global"
