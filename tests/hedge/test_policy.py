from __future__ import annotations

from decimal import Decimal

import pytest

from app.hedge.policy import Exposure, HedgePolicy, Quote


def _configure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEDGE_MIN_ABS_DELTA_USD", "50")
    monkeypatch.setenv("HEDGE_DEADBAND_USD", "25")
    monkeypatch.setenv("HEDGE_STEP_USD", "250")
    monkeypatch.setenv("HEDGE_MAX_NOTIONAL_USD", "5000")
    monkeypatch.setenv("HEDGE_MAX_SLIPPAGE_BPS", "5")
    monkeypatch.setenv("HEDGE_QUOTE_TTL_MS", "300")
    monkeypatch.setenv("HEDGE_VENUE_PREFS", '{"binance":1.0,"okx":0.9}')


def test_build_plan_sell_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    now_ms = 1_000
    policy = HedgePolicy(now_ms=lambda: now_ms)
    exposure = Exposure(symbol="BTCUSDT", usd=Decimal("600"))
    quotes = {
        "binance": Quote(
            venue="binance",
            symbol="BTCUSDT",
            bid=Decimal("20100"),
            ask=Decimal("20120"),
            ts_ms=now_ms - 100,
        ),
        "okx": Quote(
            venue="okx",
            symbol="BTCUSDT",
            bid=Decimal("20000"),
            ask=Decimal("20010"),
            ts_ms=now_ms - 50,
        ),
    }

    plan, reason = policy.build_plan(exposure, quotes)

    assert reason == "ok"
    assert plan is not None
    assert plan.notional_usd == Decimal("500")
    leg = plan.legs[0]
    assert leg.side == "sell"
    assert leg.qty > 0
    expected_limit = quotes["binance"].bid * (Decimal("1") - Decimal("0.0005"))
    assert leg.px_limit == expected_limit


def test_deadband_min_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    policy = HedgePolicy(now_ms=lambda: 0)
    exposure = Exposure(symbol="BTCUSDT", usd=Decimal("40"))

    plan, reason = policy.build_plan(exposure, {})

    assert plan is None
    assert reason == "deadband-min"


def test_no_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    now_ms = 10_000
    policy = HedgePolicy(now_ms=lambda: now_ms)
    exposure = Exposure(symbol="BTCUSDT", usd=Decimal("700"))
    quotes = {
        "binance": Quote(
            venue="binance",
            symbol="BTCUSDT",
            bid=Decimal("20000"),
            ask=Decimal("20010"),
            ts_ms=now_ms - 1_000,
        )
    }

    plan, reason = policy.build_plan(exposure, quotes)

    assert plan is None
    assert reason == "no-quotes"


def test_buy_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch)
    now_ms = 5_000
    policy = HedgePolicy(now_ms=lambda: now_ms)
    exposure = Exposure(symbol="ETHUSDT", usd=Decimal("-800"))
    quotes = {
        "binance": Quote(
            venue="binance",
            symbol="ETHUSDT",
            bid=Decimal("1500"),
            ask=Decimal("1505"),
            ts_ms=now_ms - 10,
        )
    }

    plan, reason = policy.build_plan(exposure, quotes)

    assert reason == "ok"
    assert plan is not None
    assert plan.notional_usd == Decimal("750")
    leg = plan.legs[0]
    assert leg.side == "buy"
    assert leg.qty > 0
    expected_limit = quotes["binance"].ask * (Decimal("1") + Decimal("0.0005"))
    assert leg.px_limit == expected_limit
