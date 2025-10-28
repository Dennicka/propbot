from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APPROVE_TOKEN", "pytest-approve")
os.environ.setdefault("AUTH_ENABLED", "false")

from app import ledger
from app.main import app
from app.services import runtime, approvals_store
from app.services.loop import hold_loop
from app.services.runtime import reset_for_tests
from positions import reset_positions


@pytest.fixture
def client(monkeypatch) -> TestClient:
    reset_for_tests()
    runtime.record_resume_request("tests_bootstrap", requested_by="pytest")
    runtime.approve_resume(actor="pytest")
    ledger.reset()
    monkeypatch.setattr(
        "services.opportunity_scanner.check_spread",
        lambda symbol: {
            "cheap": "binance",
            "expensive": "okx",
            "spread": 10.0,
            "spread_bps": 15.0,
        },
    )
    # ensure background loop is not running between tests
    try:
        import asyncio

        asyncio.run(hold_loop())
    except RuntimeError:
        # event loop already running in this thread; skip explicit hold
        pass
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_auth_env(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("IDEM_TTL_SEC", raising=False)
    monkeypatch.delenv("API_RATE_PER_MIN", raising=False)
    monkeypatch.delenv("API_BURST", raising=False)
    monkeypatch.setenv("TELEGRAM_ENABLE", "false")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("OPS_ALERTS_FILE", raising=False)
    monkeypatch.delenv("OPS_ALERTS_DIR", raising=False)


@pytest.fixture(autouse=True)
def reset_idem_and_rate_limiters():
    cache = getattr(app.state, "idempotency_cache", None)
    if cache is not None:
        cache.clear()
    limiter = getattr(app.state, "rate_limiter", None)
    if limiter is not None:
        limiter.set_clock(time.monotonic)
        default_limits = getattr(app.state, "default_rate_limits", (limiter.rate_per_min, limiter.burst))
        limiter.set_limits(*default_limits)


@pytest.fixture(autouse=True)
def override_positions_store(monkeypatch, tmp_path: Path):
    path = tmp_path / "hedge_positions.json"
    monkeypatch.setenv("POSITIONS_STORE_PATH", str(path))
    reset_positions()
    yield
    reset_positions()


@pytest.fixture(autouse=True)
def override_approvals_store(monkeypatch, tmp_path: Path):
    path = tmp_path / "ops_approvals.json"
    monkeypatch.setenv("OPS_APPROVALS_FILE", str(path))
    approvals_store.reset_for_tests()
    yield
    approvals_store.reset_for_tests()
