from __future__ import annotations

from fastapi.testclient import TestClient


def _auth_headers(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def test_mutations_open_when_auth_disabled(monkeypatch, client: TestClient) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    response = client.post("/api/ui/hold")
    assert response.status_code == 200


def test_mutations_require_token_when_enabled(monkeypatch, client: TestClient) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    missing = client.post("/api/ui/hold")
    assert missing.status_code == 401
    assert missing.json() == {"detail": "unauthorized"}

    wrong = client.post(
        "/api/ui/hold",
        headers=_auth_headers("bad-token"),
    )
    assert wrong.status_code == 401
    assert wrong.json() == {"detail": "unauthorized"}

    ok = client.post(
        "/api/ui/hold",
        headers=_auth_headers("secret-token"),
    )
    assert ok.status_code == 200


def test_reads_remain_public(monkeypatch, client: TestClient) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    response = client.get("/api/ui/state")
    assert response.status_code == 200

    monkeypatch.setenv("AUTH_ENABLED", "false")
    response_disabled = client.get("/api/ui/state")
    assert response_disabled.status_code == 200
