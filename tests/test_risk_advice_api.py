from typing import Dict

from fastapi.testclient import TestClient

from app.services import runtime, risk_guard
from pnl_history_store import append_snapshot


def _auth(token: str | None) -> Dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def test_risk_advice_endpoint_requires_token(monkeypatch, client: TestClient) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "90000")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "4")

    state = runtime.get_state()
    state.control.dry_run_mode = True
    state.safety.hold_active = True
    state.safety.hold_reason = risk_guard.REASON_RUNAWAY_NOTIONAL

    for pnl in (100.0, -150.0, -300.0):
        append_snapshot(
            {
                "unrealized_pnl_total": pnl,
                "total_exposure_usd_total": 65000.0,
                "open_positions": 2,
                "partial_positions": 2,
            }
        )

    unauthorized = client.get("/api/ui/risk_advice")
    assert unauthorized.status_code == 401

    authorized = client.get("/api/ui/risk_advice", headers=_auth("secret-token"))
    assert authorized.status_code == 200
    payload = authorized.json()
    assert "suggested_max_notional" in payload
    assert "suggested_max_positions" in payload
    assert payload.get("reason")
