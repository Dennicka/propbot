from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.usefixtures("reset_auth_env")
def test_universe_endpoint_requires_token(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "pytest-token")

    unauthenticated = client.get("/api/ui/universe")
    assert unauthenticated.status_code in {401, 403}

    response = client.get(
        "/api/ui/universe",
        headers={"Authorization": "Bearer pytest-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "pairs" in payload
    assert isinstance(payload["pairs"], list)
