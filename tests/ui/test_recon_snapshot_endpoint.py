from __future__ import annotations

from fastapi.testclient import TestClient


def test_recon_snapshot_endpoint_smoke(client: TestClient) -> None:
    response = client.get("/api/ui/recon/snapshot", params={"venue_id": "test_venue"})
    assert response.status_code == 200
    data = response.json()
    assert data["venue_id"] == "test_venue"
    assert "issues" in data
    assert "errors_count" in data
    assert "warnings_count" in data
