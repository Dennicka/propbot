from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.approvals.live_toggle import LiveToggleStore
from app.routers import ops_live_toggle, ui_live_approvals
from app.routers.ops_live_toggle import CurrentUser, get_current_user


def test_live_toggle_approval_flow(monkeypatch) -> None:
    store = LiveToggleStore()

    monkeypatch.setattr(ops_live_toggle, "get_live_toggle_store", lambda: store)
    monkeypatch.setattr(ui_live_approvals, "get_live_toggle_store", lambda: store)

    app = FastAPI()
    app.include_router(ops_live_toggle.router)
    app.include_router(ui_live_approvals.router, prefix="/api/ui")

    current: dict[str, CurrentUser] = {"user": CurrentUser(id="operator_one", role="operator")}

    def _current_user_override() -> CurrentUser:
        return current["user"]

    app.dependency_overrides[get_current_user] = _current_user_override

    client = TestClient(app)

    response = client.post(
        "/api/ops/live-toggle/requests",
        json={"action": "enable_live", "reason": "go live"},
    )
    assert response.status_code == 200
    payload = response.json()
    request_id = payload["id"]
    assert payload["status"] == "pending"
    assert payload["requestor_id"] == "operator_one"

    current["user"] = CurrentUser(id="operator_two", role="operator")
    response = client.post(
        f"/api/ops/live-toggle/requests/{request_id}/approve",
        json={"resolution_reason": "confirmed"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "approved"
    assert payload["approver_id"] == "operator_two"
    assert payload["resolution_reason"] == "confirmed"

    response = client.get("/api/ui/live-approvals")
    assert response.status_code == 200
    approvals = response.json()
    assert isinstance(approvals, list)
    assert approvals
    entry = approvals[0]
    assert entry["id"] == request_id
    assert entry["status"] == "approved"
