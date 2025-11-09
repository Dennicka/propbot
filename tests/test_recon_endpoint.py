from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from app.services import runtime


def test_recon_status_endpoint_normalises_payload(monkeypatch, client: TestClient) -> None:
    snapshot: Dict[str, Any] = {
        "diffs": [
            {
                "kind": "balance",
                "venue": "binance-um",
                "symbol": "USDT",
                "local": 1200,
                "remote": 1000,
                "diff_abs": 200,
                "diff_rel": 0.2,
                "severity": "WARN",
            }
        ],
        "has_warn": False,
        "has_crit": False,
        "state": "WARN",
    }

    monkeypatch.setattr(runtime, "get_reconciliation_status", lambda: snapshot)

    response = client.get("/api/ui/recon_status")
    assert response.status_code == 200
    payload = response.json()

    assert payload["has_warn"] is True
    assert payload["has_crit"] is False
    assert isinstance(payload["diffs"], list)
    assert payload["diffs"][0]["severity"] == "WARN"
    assert payload["diffs"][0]["diff_rel"] == pytest.approx(0.2)
