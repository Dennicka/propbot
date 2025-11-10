import pytest

from app.profile_config import (
    MissingSecretsError,
    ProfileConfig,
    ensure_required_secrets,
    load_profile_config,
)


def _load(name: str) -> ProfileConfig:
    return load_profile_config(name)


@pytest.mark.parametrize("profile", ["paper", "testnet", "live"])
def test_profiles_parse(profile: str) -> None:
    cfg = _load(profile)
    assert cfg.name == profile
    assert cfg.broker.primary.endpoints.rest
    assert cfg.flags.as_dict()


def test_paper_and_testnet_are_dry_run() -> None:
    assert _load("paper").dry_run is True
    assert _load("testnet").dry_run is True
    assert _load("live").dry_run is False


def test_live_profile_requires_secrets() -> None:
    live_cfg = _load("live")
    assert live_cfg.requires_secrets is True

    with pytest.raises(MissingSecretsError):
        ensure_required_secrets(live_cfg, lambda _name: {})

    def provider(name: str):
        if name == "binance":
            return {"key": "abc", "secret": "def"}
        if name == "okx":
            return {"key": "ghi", "secret": "jkl", "passphrase": "mno"}
        return {}

    ensure_required_secrets(live_cfg, provider)
