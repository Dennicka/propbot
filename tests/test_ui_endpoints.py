from __future__ import annotations

from datetime import datetime, timezone

from app import ledger
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
    assert "open_orders" in state_payload
    assert "positions" in state_payload
    assert "recon_status" in state_payload
    assert state_payload["loop"]["status"] == "HOLD"

    secret_resp = client.get("/api/ui/secret")
    assert secret_resp.status_code == 200
    secret_payload = secret_resp.json()
    assert str(secret_payload["pair"]).upper() in {"BTCUSDT", "ETHUSDT"}
    assert secret_payload["auto_loop"] is False
    assert "notional_usdt" in secret_payload

    hold_resp = client.post("/api/ui/hold")
    assert hold_resp.status_code == 200
    assert hold_resp.json()["mode"] == "HOLD"

    reset_resp = client.post("/api/ui/reset")
    assert reset_resp.status_code == 200
    assert reset_resp.json()["loop"]["status"] == "HOLD"

    resume_fail = client.post("/api/ui/resume")
    assert resume_fail.status_code == 403

    runtime_state = get_state()
    runtime_state.control.safe_mode = False
    runtime_state.control.mode = "HOLD"

    resume_resp = client.post("/api/ui/resume")
    assert resume_resp.status_code == 200
    assert resume_resp.json()["mode"] == "RUN"

    secret_after_resume = client.get("/api/ui/secret")
    assert secret_after_resume.status_code == 200
    after_payload = secret_after_resume.json()
    assert "loop" in after_payload
    assert after_payload["loop"]["status"] in {"RUN", "HOLD", "STOPPING"}

    stop_resp = client.post("/api/ui/stop")
    assert stop_resp.status_code == 200
    assert stop_resp.json()["loop"]["status"] in {"STOPPING", "HOLD"}

    runtime_state = get_state()
    runtime_state.control.environment = "testnet"
    order_id = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.2,
        price=20_000.0,
        status="submitted",
        client_ts=datetime.now(timezone.utc).isoformat(),
        exchange_ts=None,
        idemp_key="ui-cancel",
    )
    cancel_resp = client.post("/api/ui/cancel-all")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["result"]["cancelled"] >= 1
    order = ledger.get_order(order_id)
    assert order["status"] == "cancelled"

    # stop background loop to avoid leaking tasks between tests
    client.post("/api/ui/hold")
