from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def alerts_env(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "ops_alerts.json"
    monkeypatch.setenv("OPS_ALERTS_FILE", str(path))
    return path


def test_ops_alert_logging_and_endpoint(alerts_env: Path, monkeypatch, client) -> None:
    from app.opsbot import notifier

    notifier.emit_alert("hold", "Hold engaged", extra={"source": "test"})
    notifier.emit_alert("resume", "Resume processed")

    assert alerts_env.exists()
    with alerts_env.open("r", encoding="utf-8") as handle:
        entries = json.load(handle)
    assert len(entries) >= 2
    assert entries[-1]["kind"] == "resume"

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")
    response = client.get(
        "/api/ui/alerts?limit=5",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)
    assert any(entry.get("event_type") == "resume" for entry in payload)
    assert any(entry.get("message") == "Resume processed" for entry in payload)
