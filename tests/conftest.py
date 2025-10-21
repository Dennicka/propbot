from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import ledger
from app.main import app
from app.services.runtime import reset_for_tests


@pytest.fixture
def client() -> TestClient:
    reset_for_tests()
    ledger.reset()
    return TestClient(app)
