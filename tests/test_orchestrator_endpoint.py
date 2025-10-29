from __future__ import annotations

from app.orchestrator import orchestrator
from app.services.runtime import engage_safety_hold


def test_orchestrator_plan_blocks_on_hold(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    orchestrator.reset()
    engage_safety_hold("pytest-hold", source="pytest")

    response = client.get(
        "/api/ui/orchestrator_plan",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert "strategies" in payload
    assert isinstance(payload["strategies"], list)
    assert any(entry.get("decision") == "skip" and entry.get("reason") == "hold_active" for entry in payload["strategies"])
    risk_summary = payload.get("risk_gates", {})
    assert risk_summary.get("hold_active") is True
    assert risk_summary.get("risk_caps_ok") is False

    unauth = client.get("/api/ui/orchestrator_plan")
    assert unauth.status_code in {401, 403}
