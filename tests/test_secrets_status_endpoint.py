from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from app.secrets_store import SecretsStore


def _encrypt(value: str, key: str) -> str:
    raw = value.encode("utf-8")
    key_bytes = key.encode("utf-8")
    payload = bytes(byte ^ key_bytes[index % len(key_bytes)] for index, byte in enumerate(raw))
    return base64.b64encode(payload).decode("utf-8")


def _isoformat(days_offset: int) -> str:
    timestamp = datetime.now(timezone.utc) - timedelta(days=days_offset)
    return timestamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_secrets_status_endpoint(monkeypatch, tmp_path, client):
    key = "unit-test-key"
    monkeypatch.setenv("SECRETS_ENC_KEY", key)

    secrets_path = tmp_path / "secrets.json"
    secrets_payload = {
        "binance_key": _encrypt("BINANCE_KEY", key),
        "binance_secret": _encrypt("BINANCE_SECRET", key),
        "okx_key": _encrypt("OKX_KEY", key),
        "okx_secret": _encrypt("OKX_SECRET", key),
        "approve_token": "approve-me",
        "operator_tokens": {
            "alice": {"token": "alice-operator", "role": "operator"},
            "bob": {"token": "bob-viewer", "role": "viewer"},
        },
        "meta": {
            "binance_key_last_rotated": _isoformat(120),
            "okx_key_last_rotated": _isoformat(5),
        },
    }
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")

    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")

    store = SecretsStore(secrets_path=str(secrets_path))
    exchange_keys = store.get_exchange_keys()
    assert exchange_keys["binance"]["key"] == "BINANCE_KEY"
    assert exchange_keys["binance"]["secret"] == "BINANCE_SECRET"
    assert exchange_keys["okx"]["key"] == "OKX_KEY"
    assert exchange_keys["okx"]["secret"] == "OKX_SECRET"

    response = client.get(
        "/api/ui/secrets/status",
        params={"threshold_days": 90},
        headers={"Authorization": "Bearer alice-operator"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["rotation_needed"]["binance_key"] is True
    assert body["rotation_needed"]["okx_key"] is False
    assert all("token" not in entry for entry in body["operators"])
    serialized = json.dumps(body)
    assert "BINANCE_KEY" not in serialized
    assert "OKX_SECRET" not in serialized

    viewer_response = client.get(
        "/api/ui/secrets/status",
        params={"threshold_days": 90},
        headers={"Authorization": "Bearer bob-viewer"},
    )
    assert viewer_response.status_code == 403
