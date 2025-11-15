from __future__ import annotations

import pytest

from app.runtime.live_guard import LiveTradingDisabledError, LiveTradingGuard


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [
        "ALLOW_LIVE_TRADING",
        "LIVE_TRADING_ALLOWED_VENUES",
        "LIVE_TRADING_ALLOWED_STRATEGIES",
    ]:
        monkeypatch.delenv(key, raising=False)


def test_live_guard_blocks_live_when_env_flag_not_set() -> None:
    guard = LiveTradingGuard(runtime_profile="live")

    with pytest.raises(LiveTradingDisabledError) as excinfo:
        guard.ensure_live_allowed(venue_id="binance_perp", strategy_id="alpha")

    assert "ALLOW_LIVE_TRADING" in str(excinfo.value)


def test_live_guard_allows_live_when_env_flag_true_and_venue_strategy_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    monkeypatch.setenv("LIVE_TRADING_ALLOWED_VENUES", "binance_perp")
    monkeypatch.setenv("LIVE_TRADING_ALLOWED_STRATEGIES", "arb_v1")
    guard = LiveTradingGuard(runtime_profile="live")

    guard.ensure_live_allowed(venue_id="binance_perp", strategy_id="arb_v1")


def test_live_guard_blocks_not_allowed_venue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    monkeypatch.setenv("LIVE_TRADING_ALLOWED_VENUES", "okx_perp")
    guard = LiveTradingGuard(runtime_profile="live")

    with pytest.raises(LiveTradingDisabledError) as excinfo:
        guard.ensure_live_allowed(venue_id="binance_perp", strategy_id="arb_v1")

    assert "LIVE_TRADING_ALLOWED_VENUES" in str(excinfo.value)


def test_live_guard_blocks_not_allowed_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    monkeypatch.setenv("LIVE_TRADING_ALLOWED_STRATEGIES", "arb_v1")
    guard = LiveTradingGuard(runtime_profile="live")

    with pytest.raises(LiveTradingDisabledError) as excinfo:
        guard.ensure_live_allowed(venue_id="binance_perp", strategy_id="some_other")

    assert "LIVE_TRADING_ALLOWED_STRATEGIES" in str(excinfo.value)


@pytest.mark.parametrize("profile", ["paper", "testnet.binance"])
def test_live_guard_test_only_profile(profile: str) -> None:
    guard = LiveTradingGuard(runtime_profile=profile)

    with pytest.raises(LiveTradingDisabledError) as excinfo:
        guard.ensure_live_allowed(venue_id="binance_perp", strategy_id="alpha")

    assert profile in str(excinfo.value)
