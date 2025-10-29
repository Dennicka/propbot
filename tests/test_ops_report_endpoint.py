import json
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from app.audit_log import log_operator_action
from positions import create_position


@pytest.fixture
def viewer_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    secrets_path = tmp_path / "secrets.json"
    secrets_payload = {
        "operator_tokens": {
            "auditor": {"token": "viewer-token", "role": "viewer"},
        }
    }
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")
    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    return {"Authorization": "Bearer viewer-token"}


def _bootstrap_position() -> None:
    create_position(
        symbol="ETHUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=1000.0,
        entry_spread_bps=8.0,
        leverage=3.0,
        entry_long_price=1800.0,
        entry_short_price=1805.0,
        status="open",
    )


def test_ops_report_json_viewer_access(client: TestClient, viewer_headers: dict[str, str]) -> None:
    _bootstrap_position()
    log_operator_action("ops", "operator", "HOLD_ENGAGED", {"reason": "test"})

    response = client.get("/api/ui/ops_report", headers=viewer_headers)
    assert response.status_code == 200
    payload = response.json()
    assert "build_version" in payload
    assert isinstance(payload.get("strategies"), list)
    assert isinstance(payload.get("exposure"), dict)
    assert "recent_actions" in payload


def test_ops_report_csv_viewer_access(client: TestClient, viewer_headers: dict[str, str]) -> None:
    response = client.get("/api/ui/ops_report.csv", headers=viewer_headers)
    assert response.status_code == 200
    body = response.text
    assert "section,key,value" in body


def test_ops_report_requires_token(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")

    response = client.get("/api/ui/ops_report")
    assert response.status_code == 401

    response_csv = client.get("/api/ui/ops_report.csv")
    assert response_csv.status_code == 401
