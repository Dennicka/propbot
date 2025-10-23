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

    ts_now = datetime.now(timezone.utc).isoformat()
    order_id_exposure = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.1,
        price=20_000.0,
        status="filled",
        client_ts=ts_now,
        exchange_ts=ts_now,
        idemp_key="paper-exposure",
    )
    ledger.record_fill(
        order_id=order_id_exposure,
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.1,
        price=20_000.0,
        fee=0.0,
        ts=ts_now,
    )

    state_resp = client.get("/api/ui/state")
    assert state_resp.status_code == 200
    state_payload = state_resp.json()
    assert "exposures" in state_payload
    assert "pnl" in state_payload
    assert "portfolio" in state_payload
    assert "risk" in state_payload
    risk_block = state_payload["risk"]
    assert isinstance(risk_block, dict)
    assert "limits" in risk_block
    assert "current" in risk_block
    assert state_payload["exposures"], "paper environment exposures should not be empty"
    assert any(entry["symbol"].upper() == "BTCUSDT" for entry in state_payload["exposures"])
    pnl_snapshot = state_payload["pnl"]
    for key in ("realized", "unrealized", "total"):
        assert key in pnl_snapshot
    portfolio_data = state_payload["portfolio"]
    assert set(portfolio_data.keys()) >= {"balances", "positions", "pnl_totals", "notional_total"}
    assert portfolio_data["pnl_totals"].keys() >= {"realized", "unrealized", "total"}
    if portfolio_data["positions"]:
        first_pos = portfolio_data["positions"][0]
        for field in ("venue", "symbol", "qty", "notional", "entry_px", "mark_px", "upnl", "rpnl"):
            assert field in first_pos
    if portfolio_data["balances"]:
        first_balance = portfolio_data["balances"][0]
        for field in ("venue", "asset", "free", "total"):
            assert field in first_balance
    assert "open_orders" in state_payload
    assert "positions" in state_payload
    assert "recon_status" in state_payload
    assert state_payload["loop"]["status"] == "HOLD"
    assert "loop_config" in state_payload

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

    orders_resp = client.get("/api/ui/orders")
    assert orders_resp.status_code == 200
    snapshot = orders_resp.json()
    assert set(snapshot.keys()) == {"open_orders", "positions", "fills"}

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
    ledger.reset()
    ts_testnet = datetime.now(timezone.utc).isoformat()
    filled_order_id = ledger.record_order(
        venue="binance-um",
        symbol="ETHUSDT",
        side="buy",
        qty=0.5,
        price=1_800.0,
        status="filled",
        client_ts=ts_testnet,
        exchange_ts=ts_testnet,
        idemp_key="testnet-exposure",
    )
    ledger.record_fill(
        order_id=filled_order_id,
        venue="binance-um",
        symbol="ETHUSDT",
        side="buy",
        qty=0.5,
        price=1_800.0,
        fee=0.0,
        ts=ts_testnet,
    )

    testnet_state = client.get("/api/ui/state")
    assert testnet_state.status_code == 200
    testnet_payload = testnet_state.json()
    assert testnet_payload["exposures"], "testnet environment exposures should not be empty"
    assert any(entry["symbol"].upper() == "ETHUSDT" for entry in testnet_payload["exposures"])
    for key in ("realized", "unrealized", "total"):
        assert key in testnet_payload["pnl"]
    assert "portfolio" in testnet_payload
    testnet_portfolio = testnet_payload["portfolio"]
    assert testnet_portfolio["pnl_totals"].keys() >= {"realized", "unrealized", "total"}
    if testnet_portfolio["positions"]:
        assert any(pos["symbol"].upper() == "ETHUSDT" for pos in testnet_portfolio["positions"])

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
    cancel_resp = client.post("/api/ui/cancel_all")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["result"]["cancelled"] >= 1
    order = ledger.get_order(order_id)
    assert order["status"] == "cancelled"

    close_resp = client.post("/api/ui/close_exposure")
    assert close_resp.status_code in {200, 404}

    # stop background loop to avoid leaking tasks between tests
    runtime_state.control.environment = "paper"
    client.post("/api/ui/hold")


def test_kill_switch_cancels_orders(client):
    state = get_state()
    state.control.safe_mode = False
    order_id = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.1,
        price=20_000.0,
        status="submitted",
        client_ts=datetime.now(timezone.utc).isoformat(),
        exchange_ts=None,
        idemp_key="kill-switch-order",
    )
    resp = client.post("/api/ui/kill")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["safe_mode"] is True
    assert payload["mode"] == "HOLD"
    order = ledger.get_order(order_id)
    assert order["status"] == "cancelled"
