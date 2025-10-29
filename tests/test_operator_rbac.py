from __future__ import annotations

import json

from app.services.runtime import get_state


def test_operator_rbac_enforcement(monkeypatch, tmp_path, client):
    secrets_payload = {
        "operator_tokens": {
            "alice": {"token": "AAA", "role": "operator"},
            "bob": {"token": "BBB", "role": "viewer"},
        },
        "approve_token": "ZZZ",
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")

    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("APPROVE_TOKEN", "ZZZ")

    calls: list[tuple[str, str, str, object]] = []

    def _capture(name: str, role: str, action: str, details):
        calls.append((name, role, action, details))

    monkeypatch.setattr("app.routers.ui.log_operator_action", _capture)

    viewer_headers = {"Authorization": "Bearer BBB"}
    operator_headers = {"Authorization": "Bearer AAA"}

    for path in ("/api/ui/hold", "/api/ui/resume", "/api/ui/kill"):
        response = client.post(path, headers=viewer_headers)
        assert response.status_code == 403
        assert response.json()["detail"] == "forbidden"

    hold_resp = client.post("/api/ui/hold", headers=operator_headers, json={"reason": "rbac"})
    assert hold_resp.status_code == 200

    resume_request = client.post(
        "/api/ui/resume-request",
        headers=operator_headers,
        json={"reason": "ready"},
    )
    assert resume_request.status_code == 200

    resume_confirm = client.post(
        "/api/ui/resume-confirm",
        headers=operator_headers,
        json={"token": "ZZZ"},
    )
    assert resume_confirm.status_code == 200

    state = get_state()
    state.control.safe_mode = False

    resume_resp = client.post("/api/ui/resume", headers=operator_headers)
    assert resume_resp.status_code == 200

    kill_resp = client.post("/api/ui/kill", headers=operator_headers)
    assert kill_resp.status_code == 200

    assert ("bob", "viewer", "HOLD", {"status": "forbidden"}) in calls
    assert ("bob", "viewer", "RESUME", {"status": "forbidden"}) in calls
    assert ("bob", "viewer", "KILL", {"status": "forbidden"}) in calls

    assert ("alice", "operator", "HOLD", {"status": "ok"}) in calls
    assert any(entry == ("alice", "operator", "RESUME", {"status": "ok"}) for entry in calls)
    assert ("alice", "operator", "KILL", {"status": "ok"}) in calls
