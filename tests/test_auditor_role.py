import json

from app.secrets_store import reset_secrets_store_cache


def _configure_secrets(monkeypatch, tmp_path) -> None:
    payload = {
        "operator_tokens": {
            "viewer_user": {"token": "VIEWER", "role": "viewer"},
            "auditor_user": {"token": "AUDITOR", "role": "auditor"},
            "operator_user": {"token": "OPERATOR", "role": "operator"},
        },
        "approve_token": "APPROVE",
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("APPROVE_TOKEN", "APPROVE")
    reset_secrets_store_cache()


def test_auditor_read_only_access(monkeypatch, tmp_path, client):
    _configure_secrets(monkeypatch, tmp_path)

    calls: list[tuple[str, str, str, object]] = []

    def _capture(name: str, role: str, action: str, details):
        calls.append((name, role, action, details))

    monkeypatch.setattr("app.routers.ui.log_operator_action", _capture)

    viewer_headers = {"Authorization": "Bearer VIEWER"}
    auditor_headers = {"Authorization": "Bearer AUDITOR"}
    operator_headers = {"Authorization": "Bearer OPERATOR"}

    # Auditor and viewer can fetch read-only reports
    ops_report = client.get("/api/ui/ops_report", headers=auditor_headers)
    assert ops_report.status_code == 200
    audit_snapshot = client.get("/api/ui/audit_snapshot", headers=auditor_headers)
    assert audit_snapshot.status_code == 200

    viewer_ops_report = client.get("/api/ui/ops_report", headers=viewer_headers)
    assert viewer_ops_report.status_code == 200
    viewer_audit_snapshot = client.get("/api/ui/audit_snapshot", headers=viewer_headers)
    assert viewer_audit_snapshot.status_code == 200

    # Auditor cannot trigger hold; operator can
    forbidden_hold = client.post("/api/ui/hold", headers=auditor_headers)
    assert forbidden_hold.status_code == 403

    operator_hold = client.post(
        "/api/ui/hold",
        headers=operator_headers,
        json={"reason": "test", "requested_by": "pytest"},
    )
    assert operator_hold.status_code == 200

    assert any(
        entry[:3] == ("auditor_user", "auditor", "HOLD") and entry[3]["status"] == "forbidden"
        for entry in calls
    )
    assert any(
        entry[:3] == ("operator_user", "operator", "HOLD") and entry[3]["status"] == "approved"
        for entry in calls
    )

    # Auditor dashboard is read-only and hides control forms
    dashboard = client.get("/ui/dashboard", headers=auditor_headers)
    assert dashboard.status_code == 200
    html = dashboard.text
    assert "Auditor role: read only" in html
    assert "/api/ui/dashboard-hold" not in html
    assert "/api/ui/dashboard-resume-request" not in html
    assert "/api/ui/dashboard-kill" not in html

    reset_secrets_store_cache()
