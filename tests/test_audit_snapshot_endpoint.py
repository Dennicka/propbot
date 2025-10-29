from __future__ import annotations

from app.audit_log import log_operator_action
from app.version import APP_VERSION


def test_audit_snapshot_endpoint(client, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "audit-snapshot")

    unauthorized = client.get("/api/ui/audit_snapshot")
    assert unauthorized.status_code in {401, 403}

    log_operator_action("alice", "operator", "HOLD", {"status": "approved"})

    headers = {"Authorization": "Bearer audit-snapshot"}
    response = client.get("/api/ui/audit_snapshot", headers=headers)
    assert response.status_code == 200
    payload = response.json()

    assert payload["build_version"] == APP_VERSION
    assert payload["count"] >= 1
    assert isinstance(payload["entries"], list)
    assert payload["limit"] == 100
    assert any(entry.get("action") == "HOLD" for entry in payload["entries"])


