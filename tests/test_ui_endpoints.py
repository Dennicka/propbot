from __future__ import annotations

from app.services.runtime import get_state


def test_ui_state_and_controls(client):
    preview_payload = {"symbol": "ETHUSDT", "notional": 500.0}
    client.post("/api/arb/preview", json=preview_payload)

    plan_resp = client.get("/api/ui/plan/last")
    assert plan_resp.status_code == 200
    assert plan_resp.json()["last_plan"]["symbol"] == "ETHUSDT"

    state_resp = client.get("/api/ui/state")
    assert state_resp.status_code == 200
    state_payload = state_resp.json()
    assert "exposures" in state_payload
    assert "pnl" in state_payload
    assert "recon_status" in state_payload

    hold_resp = client.post("/api/ui/hold")
    assert hold_resp.status_code == 200
    assert hold_resp.json()["mode"] == "HOLD"

    resume_fail = client.post("/api/ui/resume")
    assert resume_fail.status_code == 403

    runtime_state = get_state()
    runtime_state.control.safe_mode = False
    runtime_state.control.mode = "HOLD"

    resume_resp = client.post("/api/ui/resume")
    assert resume_resp.status_code == 200
    assert resume_resp.json()["mode"] == "RUN"
