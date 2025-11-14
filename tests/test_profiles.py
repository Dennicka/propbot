from __future__ import annotations

import pytest

from app.config.profile import TradingProfile, is_live, load_profile_from_env


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
