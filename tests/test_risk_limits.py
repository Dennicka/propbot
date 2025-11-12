from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.risk.limits import RiskConfig, RiskGovernor
from app.router.smart_router import SmartRouter


def test_notional_caps() -> None:
    venue_cfg = RiskConfig(cap_per_venue={"binance": Decimal("1000")})
    venue_governor = RiskGovernor(venue_cfg)
    ok, reason = venue_governor.allow_order(
        "binance",
        "BTCUSDT",
        "alpha",
        Decimal("100"),
        Decimal("15"),
    )
    assert not ok
    assert reason == "venue_cap"
    ok, reason = venue_governor.allow_order(
        "binance",
        "BTCUSDT",
        "alpha",
        Decimal("100"),
        Decimal("9"),
    )
    assert ok
    assert reason == ""

    symbol_cfg = RiskConfig(
        cap_per_symbol={("okx", "BTC-USDT"): Decimal("900")}
    )
    symbol_governor = RiskGovernor(symbol_cfg)
    ok, reason = symbol_governor.allow_order(
        "okx",
        "BTC-USDT",
        "beta",
        Decimal("90"),
        Decimal("11"),
    )
    assert not ok
    assert reason == "symbol_cap"
    ok, reason = symbol_governor.allow_order(
        "okx",
        "BTC-USDT",
        "beta",
        Decimal("90"),
        Decimal("10"),
    )
    assert ok
    assert reason == ""

    strategy_cfg = RiskConfig(cap_per_strategy={"gamma": Decimal("800")})
    strategy_governor = RiskGovernor(strategy_cfg)
    ok, reason = strategy_governor.allow_order(
        "bybit",
        "BTCUSDT",
        "gamma",
        Decimal("100"),
        Decimal("9"),
    )
    assert not ok
    assert reason == "strategy_cap"
    ok, reason = strategy_governor.allow_order(
        "bybit",
        "BTCUSDT",
        "gamma",
        Decimal("100"),
        Decimal("7"),
    )
    assert ok
    assert reason == ""


def test_daily_loss_limit_with_cooloff() -> None:
    cfg = RiskConfig(
        daily_loss_limit=Decimal("100"),
        daily_cooloff_sec=60,
    )
    governor = RiskGovernor(cfg)
    base_now = 1_700_000_000
    governor.on_filled(
        "binance",
        "BTCUSDT",
        "alpha",
        Decimal("-40"),
        now_s=base_now,
    )
    governor.on_filled(
        "binance",
        "BTCUSDT",
        "alpha",
        Decimal("-40"),
        now_s=base_now + 10,
    )
    governor.on_filled(
        "binance",
        "BTCUSDT",
        "alpha",
        Decimal("-40"),
        now_s=base_now + 20,
    )
    ok, reason = governor.allow_order(
        "binance",
        "BTCUSDT",
        "alpha",
        Decimal("100"),
        Decimal("1"),
        now_s=base_now + 30,
    )
    assert not ok
    assert reason == "daily_cooloff"
    ok, reason = governor.allow_order(
        "binance",
        "BTCUSDT",
        "alpha",
        Decimal("100"),
        Decimal("1"),
        now_s=base_now + 81,
    )
    assert ok
    assert reason == ""


def test_consecutive_rejects_trigger_cooloff() -> None:
    cfg = RiskConfig(max_consecutive_rejects=2, rejects_cooloff_sec=60)
    governor = RiskGovernor(cfg)
    base_now = 1_700_000_000
    governor.on_reject("okx", "BTC-USDT", "beta", now_s=base_now)
    governor.on_reject("okx", "BTC-USDT", "beta", now_s=base_now + 1)
    ok, reason = governor.allow_order(
        "okx",
        "BTC-USDT",
        "beta",
        Decimal("50"),
        Decimal("1"),
        now_s=base_now + 2,
    )
    assert not ok
    assert reason == "key_cooloff"
    ok, reason = governor.allow_order(
        "okx",
        "BTC-USDT",
        "beta",
        Decimal("50"),
        Decimal("1"),
        now_s=base_now + 62,
    )
    assert ok
    assert reason == ""


def test_risk_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FF_RISK_LIMITS", raising=False)
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    state = SimpleNamespace(config=SimpleNamespace(data=None))
    router = SmartRouter(state=state, market_data=SimpleNamespace())
    assert router._risk_governor is None
