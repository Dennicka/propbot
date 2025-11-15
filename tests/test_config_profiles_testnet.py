from __future__ import annotations

import pytest

from app.config.loader import load_app_config
from app.services.runtime import resolve_profile_config_path


@pytest.mark.parametrize(
    "config_path, venue_id",
    [
        ("configs/config.testnet.binance.yaml", "binance_um"),
        ("configs/config.testnet.okx.yaml", "okx_perp"),
        ("configs/config.testnet.bybit.yaml", "bybit_perp"),
    ],
)
def test_testnet_configs_load(config_path: str, venue_id: str) -> None:
    loaded = load_app_config(config_path)
    derivatives = loaded.data.derivatives
    assert derivatives is not None, "derivatives section must be present"
    assert derivatives.venues, "at least one venue must be configured"
    venue = next((entry for entry in derivatives.venues if entry.id == venue_id), None)
    assert venue is not None, f"venue {venue_id} should be configured"
    assert venue.testnet is True
    assert venue.routing.rest
    assert venue.routing.ws
    if venue.api_key_env:
        assert isinstance(venue.api_key_env, str) and venue.api_key_env
    if venue.api_secret_env:
        assert isinstance(venue.api_secret_env, str) and venue.api_secret_env


@pytest.mark.parametrize(
    "profile, expected",
    [
        ("paper", "configs/config.paper.yaml"),
        ("testnet", "configs/config.testnet.yaml"),
        ("testnet.binance", "configs/config.testnet.binance.yaml"),
        ("testnet.okx", "configs/config.testnet.okx.yaml"),
        ("testnet.bybit", "configs/config.testnet.bybit.yaml"),
        ("live", "configs/config.live.yaml"),
        ("TESTNET.BINANCE", "configs/config.testnet.binance.yaml"),
        ("unknown", "configs/config.paper.yaml"),
    ],
)
def test_resolve_profile_config_path(profile: str, expected: str) -> None:
    assert resolve_profile_config_path(profile) == expected
