from decimal import Decimal

import pytest

from app.config.trading_profiles import ExecutionProfile, load_profile
from app.services.trading_profile import get_trading_profile, reset_trading_profile_cache


def setup_function(function) -> None:
    reset_trading_profile_cache()


def teardown_function(function) -> None:
    reset_trading_profile_cache()


def test_load_profile_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_PROFILE", raising=False)
    profile = load_profile()
    assert profile.name == ExecutionProfile.PAPER.value
    assert profile.allow_new_orders is True
    assert profile.max_notional_per_order == Decimal("1000")


def test_load_profile_env_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_PROFILE", "testnet")
    profile = load_profile()
    assert profile.name == ExecutionProfile.TESTNET.value
    assert profile.max_notional_per_symbol == Decimal("25000")


def test_invalid_profile_falls_back_to_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_PROFILE", "invalid")
    profile = load_profile()
    assert profile.name == ExecutionProfile.PAPER.value


def test_get_trading_profile_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_PROFILE", "live")
    reset_trading_profile_cache()
    profile = get_trading_profile()
    assert profile.name == ExecutionProfile.LIVE.value
    assert profile.max_notional_global == Decimal("5000000")
