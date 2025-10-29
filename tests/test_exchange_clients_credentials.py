"""Tests that exchange clients load API keys from ``SecretsStore``."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from app.secrets_store import reset_secrets_store_cache


@pytest.fixture
def secrets_store_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    payload = {
        "binance_key": "store-binance-key",
        "binance_secret": "store-binance-secret",
        "okx_key": "store-okx-key",
        "okx_secret": "store-okx-secret",
        "okx_passphrase": "store-okx-passphrase",
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    reset_secrets_store_cache()
    yield secrets_path
    reset_secrets_store_cache()


def test_binance_client_prefers_secrets_store(
    secrets_store_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BINANCE_API_KEY", "env-binance-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "env-binance-secret")

    binance_module = importlib.import_module("exchanges.binance_futures")
    importlib.reload(binance_module)

    client = binance_module.BinanceFuturesClient()

    assert client.api_key == "store-binance-key"
    assert client.api_secret == "store-binance-secret"


def test_okx_client_prefers_secrets_store(
    secrets_store_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OKX_API_KEY", "env-okx-key")
    monkeypatch.setenv("OKX_API_SECRET", "env-okx-secret")
    monkeypatch.setenv("OKX_API_PASSPHRASE", "env-okx-passphrase")

    okx_module = importlib.import_module("exchanges.okx_futures")
    importlib.reload(okx_module)

    client = okx_module.OKXFuturesClient()

    assert client.api_key == "store-okx-key"
    assert client.api_secret == "store-okx-secret"
    assert client.passphrase == "store-okx-passphrase"
