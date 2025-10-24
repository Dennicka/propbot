from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import ledger
from app.main import app
from app.services.loop import hold_loop
from app.services.runtime import reset_for_tests


@pytest.fixture
def client() -> TestClient:
    reset_for_tests()
    ledger.reset()
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
