from datetime import datetime, timezone
from pathlib import Path


def test_audit_log_requires_token(monkeypatch, client, tmp_path: Path) -> None:
    monkeypatch.setenv("OPS_ALERTS_FILE", str(tmp_path / "ops_alerts.json"))
    monkeypatch.setenv("OPS_APPROVALS_FILE", str(tmp_path / "ops_approvals.json"))
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(tmp_path / "runtime_state.json"))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "ops-token")

    from app.opsbot import notifier
    from app.services import approvals_store
    from app.runtime_state_store import write_runtime_payload

    notifier.emit_alert(
        "risk_guard_force_hold",
        "Risk throttle engaged",
        extra={"reason": "breach", "source": "risk_guard"},
    )

    approvals_store.create_request(
        "resume",
        requested_by="alice",
        parameters={"reason": "Ready to resume"},
    )

    write_runtime_payload(
        {
            "incidents": [
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "kind": "manual_hold",
                    "details": {"reason": "test", "source": "pytest"},
                }
            ]
        }
    )

    response = client.get("/api/ui/audit_log")
    assert response.status_code in {401, 403}

    authed = client.get(
        "/api/ui/audit_log",
        headers={"Authorization": "Bearer ops-token"},
    )
    assert authed.status_code == 200
    payload = authed.json()
    assert isinstance(payload.get("events"), list)
    assert payload.get("events"), "expected merged audit events"
    sample = payload["events"][0]
    for key in ("timestamp", "actor", "action", "status", "reason"):
        assert key in sample
