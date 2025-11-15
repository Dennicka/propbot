from __future__ import annotations

import pytest

from app.config.profile import (
    TradingProfile,
    is_live,
    is_testnet,
    load_profile_from_env,
    normalise_profile_category,
    is_testnet_name,
)


@pytest.mark.parametrize(
    "env_value, expected",
    [
        (
            None,
            TradingProfile(
                name="paper",
                allow_trading=True,
                strict_flags=False,
                is_canary=False,
                display_name="paper",
            ),
        ),
        (
            "paper",
            TradingProfile(
                name="paper",
                allow_trading=True,
                strict_flags=False,
                is_canary=False,
                display_name="paper",
            ),
        ),
        (
            "testnet",
            TradingProfile(
                name="testnet",
                allow_trading=True,
                strict_flags=True,
                is_canary=False,
                display_name="testnet",
            ),
        ),
        (
            "testnet.binance",
            TradingProfile(
                name="testnet.binance",
                allow_trading=True,
                strict_flags=True,
                is_canary=False,
                display_name="testnet.binance",
            ),
        ),
        (
            "testnet.okx",
            TradingProfile(
                name="testnet.okx",
                allow_trading=True,
                strict_flags=True,
                is_canary=False,
                display_name="testnet.okx",
            ),
        ),
        (
            "testnet.bybit",
            TradingProfile(
                name="testnet.bybit",
                allow_trading=True,
                strict_flags=True,
                is_canary=False,
                display_name="testnet.bybit",
            ),
        ),
        (
            "live",
            TradingProfile(
                name="live",
                allow_trading=True,
                strict_flags=True,
                is_canary=False,
                display_name="live",
            ),
        ),
        (
            "LiVe",
            TradingProfile(
                name="live",
                allow_trading=True,
                strict_flags=True,
                is_canary=False,
                display_name="live",
            ),
        ),
        (
            "unknown",
            TradingProfile(
                name="paper",
                allow_trading=True,
                strict_flags=False,
                is_canary=False,
                display_name="paper",
            ),
        ),
    ],
)
def test_load_profile_from_env(monkeypatch: pytest.MonkeyPatch, env_value, expected):
    if env_value is None:
        monkeypatch.delenv("EXEC_PROFILE", raising=False)
    else:
        monkeypatch.setenv("EXEC_PROFILE", env_value)
    monkeypatch.delenv("CANARY_MODE", raising=False)
    monkeypatch.delenv("CANARY_PROFILE_NAME", raising=False)
    profile = load_profile_from_env()
    assert profile == expected


def test_is_live_helper():
    assert is_live(
        TradingProfile(
            name="live",
            allow_trading=True,
            strict_flags=True,
            is_canary=False,
            display_name="live",
        )
    )
    assert not is_live(
        TradingProfile(
            name="paper",
            allow_trading=True,
            strict_flags=False,
            is_canary=False,
            display_name="paper",
        )
    )


def test_is_testnet_helper():
    assert is_testnet(
        TradingProfile(
            name="testnet.binance",
            allow_trading=True,
            strict_flags=True,
            is_canary=False,
            display_name="testnet.binance",
        )
    )
    assert not is_testnet(
        TradingProfile(
            name="paper",
            allow_trading=True,
            strict_flags=False,
            is_canary=False,
            display_name="paper",
        )
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, "paper"),
        ("paper", "paper"),
        ("testnet", "testnet"),
        ("testnet.binance", "testnet"),
        ("testnet.okx", "testnet"),
        ("testnet.bybit", "testnet"),
        ("live", "live"),
        ("LiVe", "live"),
        ("unknown", "paper"),
    ],
)
def test_normalise_profile_category(raw, expected):
    assert normalise_profile_category(raw) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, False),
        ("", False),
        ("paper", False),
        ("testnet", True),
        ("testnet.binance", True),
        ("TESTNET.OKX", True),
        ("live", False),
    ],
)
def test_is_testnet_name(value, expected):
    assert is_testnet_name(value) is expected
