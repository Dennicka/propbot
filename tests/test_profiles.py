import pytest

from app.profile_config import (
    MissingSecretsError,
    ProfileConfig,
    ensure_required_secrets,
    load_profile_config,
)
from app.config.profiles import (
    ProfileSafetyError,
    RuntimeProfile,
    apply_profile_environment,
    ensure_live_prerequisites,
)
from app.secrets_store import reset_secrets_store_cache

import json


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


def test_apply_profile_environment_sets_expected_defaults() -> None:
    paper_cfg = _load("paper")
    env: dict[str, str] = {}
    updates = apply_profile_environment(RuntimeProfile.PAPER, paper_cfg, environ=env)
    assert env["PROFILE"] == "paper"
    assert env["SAFE_MODE"] == "true"
    assert env["DRY_RUN_ONLY"] == "true"
    assert env["HEDGE_ENABLED"] == "false"
    assert updates == env


def test_live_prerequisites_require_guards(monkeypatch, tmp_path) -> None:
    reset_secrets_store_cache()
    live_cfg = _load("live")
    secrets_path = tmp_path / "secrets.json"
    secrets_payload = {
        "binance_key": "abc",
        "binance_secret": "def",
        "okx_key": "ghi",
        "okx_secret": "jkl",
        "okx_passphrase": "mno",
    }
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")
    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    env_updates = apply_profile_environment(RuntimeProfile.LIVE, live_cfg, environ={})
    for key, value in env_updates.items():
        monkeypatch.setenv(key, value)

    ensure_live_prerequisites(live_cfg)

    monkeypatch.setenv("FEATURE_SLO", "false")
    with pytest.raises(ProfileSafetyError) as excinfo:
        ensure_live_prerequisites(live_cfg)
    assert "SLO" in str(excinfo.value)


def test_live_prerequisites_fail_without_secrets(monkeypatch) -> None:
    reset_secrets_store_cache()
    live_cfg = _load("live")
    env_updates = apply_profile_environment(RuntimeProfile.LIVE, live_cfg, environ={})
    for key, value in env_updates.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("SECRETS_STORE_PATH", raising=False)
    with pytest.raises(ProfileSafetyError):
        ensure_live_prerequisites(live_cfg)
