from __future__ import annotations

import json

from app.audit_log import list_recent_operator_actions
from app.secrets_store import reset_secrets_store_cache


def _setup_secrets(monkeypatch, tmp_path) -> None:
    payload = {
        "operator_tokens": {
            "alice": {"token": "AAA", "role": "operator"},
            "bob": {"token": "BBB", "role": "operator"},
            "viewer": {"token": "VVV", "role": "viewer"},
        },
        "approve_token": "ZZZ",
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.setenv("APPROVE_TOKEN", "ZZZ")
    reset_secrets_store_cache()


def test_two_step_kill_flow(monkeypatch, tmp_path, client) -> None:
    _setup_secrets(monkeypatch, tmp_path)

    async def _fake_cancel_all_orders(venue=None, *, correlation_id=None):
        return {"orders_cancelled": True, "correlation_id": correlation_id}

    monkeypatch.setattr("app.routers.ui.cancel_all_orders", _fake_cancel_all_orders)

    viewer_headers = {"Authorization": "Bearer VVV"}
    operator_a = {"Authorization": "Bearer AAA"}
    operator_b = {"Authorization": "Bearer BBB"}

    before = list_recent_operator_actions()

    forbidden = client.post(
        "/api/ui/kill-request",
        headers=viewer_headers,
        json={"reason": "viewer"},
    )
    assert forbidden.status_code == 403

    log_after_viewer = list_recent_operator_actions()
    assert len(log_after_viewer) >= len(before) + 1
    assert log_after_viewer[-1]["action"] == "KILL_REQUEST"
    assert log_after_viewer[-1]["role"] == "viewer"
    assert log_after_viewer[-1]["details"]["status"] == "forbidden"

    requested = client.post(
        "/api/ui/kill-request",
        headers=operator_a,
        json={"reason": "maintenance", "requested_by": "alice"},
    )
    assert requested.status_code == 202
    request_id = requested.json()["request_id"]

    log_after_request = list_recent_operator_actions()
    assert log_after_request[-1]["action"] == "KILL_REQUEST"
    assert log_after_request[-1]["role"] == "operator"
    assert log_after_request[-1]["details"]["status"] == "requested"

    approval = client.post(
        "/api/ui/kill",
        headers=operator_b,
        json={"request_id": request_id, "token": "ZZZ", "actor": "bob"},
    )
    assert approval.status_code == 200
    payload = approval.json()
    assert payload["safe_mode"] is True
    assert payload["mode"] == "HOLD"
    assert payload["request_id"] == request_id

    log_after_approve = list_recent_operator_actions()
    assert log_after_approve[-1]["action"] == "KILL_APPROVE"
    assert log_after_approve[-1]["details"]["status"] == "approved"

    reset_secrets_store_cache()
