from pathlib import Path


def test_audit_export_requires_token(monkeypatch, client, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    audit_path = tmp_path / "audit.log"

    import app.audit_log as audit_log

    monkeypatch.setattr("app.audit_log._AUDIT_LOG_PATH", audit_path, raising=False)
    audit_log._IN_MEMORY_LOG.clear()

    audit_log.log_operator_action(
        "alice",
        "operator",
        "HOLD_REQUESTED",
        details={"reason": "maintenance"},
    )
    audit_log.log_operator_action(
        "alice",
        "operator",
        "RESUME_REQUESTED",
        details={"reason": "checks_passed"},
    )

    response = client.get("/api/ui/audit/export")
    assert response.status_code == 401

    unauthorized = client.get(
        "/api/ui/audit/export",
        headers={"Authorization": "Bearer invalid"},
    )
    assert unauthorized.status_code in (401, 403)

    response = client.get(
        "/api/ui/audit/export",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "events" in payload
    assert len(payload["events"]) >= 2
    last_event = payload["events"][-1]
    assert last_event["action"] == "RESUME_REQUESTED"
    assert last_event["operator_name"] == "alice"
    assert last_event["details"].get("reason") == "checks_passed"
