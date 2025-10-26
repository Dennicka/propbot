import json

import pytest

from app.services import runtime


@pytest.mark.parametrize(
    "env_var",
    [
        "API_TOKEN",
        "BINANCE_LV_API_KEY",
        "TELEGRAM_BOT_TOKEN",
    ],
)
def test_ui_endpoints_redact_secrets(client, monkeypatch: pytest.MonkeyPatch, env_var: str) -> None:
    secret = f"secret-{env_var.lower()}"
    monkeypatch.setenv(env_var, secret)
    runtime.reset_for_tests()
    state = runtime.get_state()
    state.incidents.append({"detail": secret})
    state.control.approvals["ops"] = secret
    state.loop.last_plan = {"token": secret}
    state.loop.last_execution = {"result": secret}
    state.loop.last_error = secret
    guard = state.guards.get("cancel_on_disconnect")
    if guard is not None:
        guard.summary = f"leak {secret}"

    state_response = client.get("/api/ui/state")
    assert state_response.status_code == 200
    state_payload = state_response.json()
    serialised_state = json.dumps(state_payload)
    assert secret not in serialised_state
    assert "***redacted***" in serialised_state

    secret_response = client.get("/api/ui/secret")
    assert secret_response.status_code == 200
    secret_payload = secret_response.json()
    serialised_secret = json.dumps(secret_payload)
    assert secret not in serialised_secret
    assert "***redacted***" in serialised_secret

    status_response = client.get("/api/ui/status/overview")
    assert status_response.status_code == 200
    status_payload = status_response.json()
    serialised_status = json.dumps(status_payload)
    assert secret not in serialised_status
    assert "***redacted***" in serialised_status

    runtime.reset_for_tests()
