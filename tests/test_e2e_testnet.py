from __future__ import annotations

import os
from typing import List

import pytest

from scripts import testnet_smoke

REQUIRED_SECRETS: List[str] = [
    "BINANCE_UM_API_KEY_TESTNET",
    "BINANCE_UM_API_SECRET_TESTNET",
    "OKX_API_KEY_TESTNET",
    "OKX_API_SECRET_TESTNET",
    "OKX_API_PASSPHRASE_TESTNET",
]


@pytest.mark.e2e
def test_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = [name for name in REQUIRED_SECRETS if not os.getenv(name)]
    if missing:
        pytest.skip("testnet secrets not configured")

    monkeypatch.setenv("MODE", "testnet")
    monkeypatch.setenv("SAFE_MODE", "true")
    monkeypatch.setenv("POST_ONLY", "true")
    monkeypatch.setenv("REDUCE_ONLY", "true")

    result = testnet_smoke.run_smoke(dry_run=True, log_path=None)

    assert result["mode"] == "testnet"
    assert result["safe_mode"] is True
    assert result["execution"]["executed"] is False
    assert isinstance(result["edges"], list)
    assert len(result["edges"]) >= 0
    assert isinstance(result["preflight"], dict)
