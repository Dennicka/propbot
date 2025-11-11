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

    forbidden_paths = (
        ("/api/ui/hold", {}),
        ("/api/ui/resume-request", {"reason": "view"}),
        ("/api/ui/kill-request", {"reason": "panic"}),
    )
    for path, payload in forbidden_paths:
        response = client.post(path, headers=viewer_headers, json=payload or None)
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
    request_id = resume_request.json()["resume_request"]["id"]

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

    kill_request = client.post(
        "/api/ui/kill-request",
        headers=operator_headers,
        json={"reason": "emergency", "requested_by": "alice"},
    )
    assert kill_request.status_code == 202
    kill_request_id = kill_request.json()["request_id"]

    async def _fake_cancel_all_orders(venue=None, *, correlation_id=None):
        return {"orders_cancelled": True, "correlation_id": correlation_id}

    monkeypatch.setattr("app.routers.ui.cancel_all_orders", _fake_cancel_all_orders)

    kill_resp = client.post(
        "/api/ui/kill",
        headers=operator_headers,
        json={"request_id": kill_request_id, "token": "ZZZ"},
    )
    assert kill_resp.status_code == 200

    assert ("bob", "viewer", "HOLD", {"status": "forbidden"}) in calls
    assert any(
        entry
        for entry in calls
        if entry[:3] == ("bob", "viewer", "RESUME_REQUEST") and entry[3]["status"] == "forbidden"
    )
    assert any(
        entry
        for entry in calls
        if entry[:3] == ("bob", "viewer", "KILL_REQUEST") and entry[3]["status"] == "forbidden"
    )

    assert (
        "alice",
        "operator",
        "HOLD",
        {"status": "approved", "reason": "rbac", "requested_by": "ui"},
    ) in calls
    assert any(
        entry[:3] == ("alice", "operator", "RESUME_REQUEST") and entry[3]["status"] == "requested"
        for entry in calls
    )
    assert any(
        entry[:3] == ("alice", "operator", "RESUME_APPROVE") and entry[3]["status"] == "approved"
        for entry in calls
    )
    assert any(
        entry[:3] == ("alice", "operator", "RESUME_EXECUTE") and entry[3]["status"] == "approved"
        for entry in calls
    )
    assert any(
        entry[:3] == ("alice", "operator", "KILL_REQUEST") and entry[3]["status"] == "requested"
        for entry in calls
    )
    assert any(
        entry[:3] == ("alice", "operator", "KILL_APPROVE") and entry[3]["status"] == "approved"
        for entry in calls
    )
