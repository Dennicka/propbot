import time

import pytest
from fastapi.testclient import TestClient

from app.server_ws import app
from app.readiness.live import registry


client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    registry.items.clear()
    yield
    registry.items.clear()


def test_live_readiness_empty() -> None:
    response = client.get("/live-readiness")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "components": {}}


def test_live_readiness_fresh_router() -> None:
    now = int(time.time())
    registry.beat("router", ts=now)

    response = client.get("/live-readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["components"]["router"]["stale"] is False


def test_live_readiness_stale_noncritical() -> None:
    now = int(time.time())
    registry.beat("telemetry", ts=now - 1000)

    response = client.get("/live-readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["components"]["telemetry"]["stale"] is True


def test_live_readiness_stale_critical() -> None:
    now = int(time.time())
    registry.beat("market_data", ts=now - 1000)

    response = client.get("/live-readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "down"
    assert payload["components"]["market_data"]["stale"] is True
