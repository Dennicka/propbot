from pathlib import Path


def test_audit_export_requires_token(monkeypatch, client, tmp_path: Path) -> None:
    path = tmp_path / "ops_alerts.json"
    monkeypatch.setenv("OPS_ALERTS_FILE", str(path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    from app.opsbot import notifier

    notifier.emit_alert("test_event", "Test alert", extra={"source": "pytest"})

    response = client.get("/api/ui/audit/export")
    assert response.status_code == 401

    response = client.get(
        "/api/ui/audit/export",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "events" in payload
    assert payload["events"], "expected at least one audit entry"
    last_event = payload["events"][-1]
    assert last_event["kind"] == "test_event"
    assert last_event.get("extra", {}).get("source") == "pytest"
