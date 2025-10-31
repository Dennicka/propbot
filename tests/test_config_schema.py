from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from app.config.loader import validate_payload
from app.main import app
from app.services.runtime import get_state


def test_validate_payload_success() -> None:
    sample = Path("configs/config.paper.yaml")
    data = yaml.safe_load(sample.read_text(encoding="utf-8"))
    errors = validate_payload(data)
    assert errors == []


def test_validate_payload_errors() -> None:
    errors = validate_payload({"profile": 123})
    assert errors


def test_config_validate_get(tmp_path) -> None:
    client = TestClient(app)
    state = get_state()
    original_path = state.config.path
    try:
        config_copy = tmp_path / "config.yaml"
        config_copy.write_text(Path(original_path).read_text(encoding="utf-8"), encoding="utf-8")
        state.config.path = config_copy
        response = client.get("/api/ui/config/validate")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["errors"] == []
        config_copy.write_text("profile: 123", encoding="utf-8")
        response = client.get("/api/ui/config/validate")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["errors"]
    finally:
        state.config.path = original_path


def test_config_validate_post_errors() -> None:
    client = TestClient(app)
    response = client.post("/api/ui/config/validate", json={"yaml_text": "profile: 123"})
    assert response.status_code == 400
    body = response.json()
    assert body["detail"]["ok"] is False
    assert body["detail"]["errors"]
