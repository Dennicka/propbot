from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.server_ws import app
from app.services.runtime import reset_for_tests


@pytest.fixture
def client() -> TestClient:
    reset_for_tests()
    return TestClient(app)
