from __future__ import annotations

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
