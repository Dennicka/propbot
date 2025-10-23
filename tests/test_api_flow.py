from __future__ import annotations

from app.services.runtime import get_state


def test_preview_and_execute_flow(client):
    preview_payload = {"symbol": "BTCUSDT", "notional": 1000.0, "used_slippage_bps": 2}
    resp = client.post("/api/arb/preview", json=preview_payload)
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["symbol"] == "BTCUSDT"
    assert plan["viable"] in {True, False}

    # ensure SAFE_MODE blocks execution initially
    block_resp = client.post("/api/arb/execute", json=plan)
    assert block_resp.status_code == 403

    # disable SAFE_MODE and enable dry-run to simulate execution
    state = get_state()
    state.control.safe_mode = False
    state.control.dry_run = True

    exec_resp = client.post("/api/arb/execute", json=plan)
    assert exec_resp.status_code == 200
    payload = exec_resp.json()
    assert payload["symbol"] == "BTCUSDT"
    assert isinstance(payload.get("orders"), list)
    assert isinstance(payload.get("exposures"), list)
    assert "pnl_summary" in payload
    assert payload["pnl_summary"].keys() >= {"realized", "unrealized", "total"}


def test_preview_accepts_pair_alias(client):
    payload = {"pair": "ETHUSDT", "notional": 200.0}
    resp = client.post("/api/arb/preview", json=payload)
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["symbol"] == "ETHUSDT"


def test_preview_rejected_by_risk_limits(client):
    state = get_state()
    state.risk.limits.max_position_usdt = {"BTCUSDT": 50.0}
    preview_payload = {"symbol": "BTCUSDT", "notional": 200.0, "used_slippage_bps": 2}
    resp = client.post("/api/arb/preview", json=preview_payload)
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["viable"] is False
    assert "max_position_usdt" in (plan.get("reason") or "")

    state.control.safe_mode = False
    execute_resp = client.post("/api/arb/execute", json=plan)
    assert execute_resp.status_code == 422


def test_execute_accepts_preview_payload_with_extra_fields(client):
    state = get_state()
    state.control.safe_mode = False
    state.control.dry_run = True
    preview_payload = {"symbol": "BTCUSDT", "notional": 500.0, "used_slippage_bps": 2}
    preview_resp = client.post("/api/arb/preview", json=preview_payload)
    assert preview_resp.status_code == 200
    plan = preview_resp.json()
    plan["viable"] = False
    plan["extra_payload"] = {"from": "preview"}
    exec_resp = client.post("/api/arb/execute", json=plan)
    assert exec_resp.status_code == 200
    body = exec_resp.json()
    assert body["symbol"] == "BTCUSDT"
    assert isinstance(body.get("orders"), list)
    assert isinstance(body.get("exposures"), list)
    assert body["pnl_summary"].keys() >= {"realized", "unrealized", "total"}


def test_preview_prioritises_risk_reason_over_spread(client):
    state = get_state()
    state.risk.limits.max_position_usdt = {"BTCUSDT": 50.0}
    preview_payload = {"symbol": "BTCUSDT", "notional": 100.0, "used_slippage_bps": 500}
    resp = client.post("/api/arb/preview", json=preview_payload)
    assert resp.status_code == 200
    plan = resp.json()
    assert plan["viable"] is False
    reason = plan.get("reason") or ""
    assert reason.startswith("risk:max_position_usdt")
    if "; " in reason:
        assert "spread" in reason.split("; ", 1)[1]
