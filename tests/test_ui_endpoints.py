from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from app import ledger
from app.broker.binance import BinanceTestnetBroker
from app.services import runtime
from app.services.runtime import get_state
from app.secrets_store import reset_secrets_store_cache
from app.risk import accounting as risk_accounting, core as risk_core


def test_ui_state_and_controls(client, monkeypatch):
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
    assert "risk_blocked" in state_payload
    assert "risk_reasons" in state_payload
    assert "control" in state_payload
    assert state_payload["control"]["safe_mode"] is True
    events_block = state_payload["events"]
    assert isinstance(events_block, dict)
    assert "items" in events_block
    assert events_block.get("limit") == 100
    assert events_block.get("offset") == 0
    assert events_block.get("order") in {"asc", "desc"}
    assert "total" in events_block
    assert len(events_block["items"]) <= events_block["limit"]
    assert events_block["total"] >= len(events_block["items"])
    risk_block = state_payload["risk"]
    assert isinstance(risk_block, dict)
    assert "limits" in risk_block
    assert "current" in risk_block
    assert state_payload["exposures"], "paper environment exposures should not be empty"
    assert all("venue_type" in entry for entry in state_payload["exposures"])
    assert any(entry["symbol"].upper() == "BTCUSDT" for entry in state_payload["exposures"])
    pnl_snapshot = state_payload["pnl"]
    for key in ("realized", "unrealized", "total"):
        assert key in pnl_snapshot
    portfolio_data = state_payload["portfolio"]
    assert set(portfolio_data.keys()) >= {"balances", "positions", "pnl_totals", "notional_total"}
    assert portfolio_data["pnl_totals"].keys() >= {"realized", "unrealized", "total"}
    if portfolio_data["positions"]:
        first_pos = portfolio_data["positions"][0]
        for field in ("venue", "venue_type", "symbol", "qty", "notional", "entry_px", "mark_px", "upnl", "rpnl"):
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

    hold_resp = client.post("/api/ui/hold", json={"reason": "test", "requested_by": "pytest"})
    assert hold_resp.status_code == 200
    hold_payload = hold_resp.json()
    assert hold_payload["mode"] == "HOLD"
    assert hold_payload["hold_active"] is True

    reset_resp = client.post("/api/ui/reset")
    assert reset_resp.status_code == 200
    assert reset_resp.json()["loop"]["status"] == "HOLD"

    locked_resume = client.post("/api/ui/resume")
    assert locked_resume.status_code == 423

    resume_request = client.post(
        "/api/ui/resume-request",
        json={"reason": "ready", "requested_by": "pytest"},
    )
    assert resume_request.status_code == 200
    assert resume_request.json()["hold_active"] is True

    missing_token = client.post(
        "/api/ui/resume-confirm",
        json={"token": "unit-test-token", "actor": "pytest"},
    )
    assert missing_token.status_code == 401
    assert missing_token.json()["detail"] == "invalid_token"

    monkeypatch.setenv("APPROVE_TOKEN", "unit-test-token")

    invalid_token = client.post(
        "/api/ui/resume-confirm",
        json={"token": "wrong-token", "actor": "pytest"},
    )
    assert invalid_token.status_code == 401
    assert invalid_token.json()["detail"] == "invalid_token"

    resume_confirm = client.post(
        "/api/ui/resume-confirm",
        json={"token": "unit-test-token", "actor": "pytest"},
    )
    assert resume_confirm.status_code == 200
    confirm_payload = resume_confirm.json()
    assert confirm_payload["hold_cleared"] is True
    assert confirm_payload["hold_active"] is False

    resume_fail_safe = client.post("/api/ui/resume")
    assert resume_fail_safe.status_code == 403

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
    assert stop_resp.status_code in {200, 429}
    if stop_resp.status_code == 200:
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
    assert all("venue_type" in entry for entry in testnet_payload["exposures"])
    assert any(entry["symbol"].upper() == "ETHUSDT" for entry in testnet_payload["exposures"])
    for key in ("realized", "unrealized", "total"):
        assert key in testnet_payload["pnl"]
    assert "portfolio" in testnet_payload
    assert "risk_blocked" in testnet_payload
    assert "risk_reasons" in testnet_payload
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
    assert cancel_resp.status_code in {200, 423, 429}
    if cancel_resp.status_code == 200:
        assert cancel_resp.json()["result"]["cancelled"] >= 1
        order = ledger.get_order(order_id)
        assert order["status"] == "cancelled"

    close_resp = client.post("/api/ui/close_exposure")
    assert close_resp.status_code in {200, 404, 429}

    # stop background loop to avoid leaking tasks between tests
    runtime_state.control.environment = "paper"
    client.post("/api/ui/hold")


def test_ui_state_uses_binance_account_when_testnet(client, monkeypatch):
    runtime.reset_for_tests()
    ledger.reset()
    state = get_state()
    state.control.environment = "testnet"
    state.control.safe_mode = True
    state.control.dry_run = False

    sample_state = {
        "balances": [
            {"venue": "binance-um", "asset": "USDT", "free": 950.0, "total": 1000.0},
        ],
        "positions": [
            {
                "venue": "binance-um",
                "venue_type": "binance-testnet",
                "symbol": "BTCUSDT",
                "qty": 0.01,
                "avg_entry": 25000.0,
                "mark_price": 25100.0,
                "notional": 251.0,
            }
        ],
    }

    async def fake_state(self):  # pragma: no cover - deterministic stub
        return sample_state

    async def fake_fills(self, since=None):  # pragma: no cover - deterministic stub
        return []

    monkeypatch.setattr(BinanceTestnetBroker, "get_account_state", fake_state)
    monkeypatch.setattr(BinanceTestnetBroker, "get_fills", fake_fills)

    response = client.get("/api/ui/state")
    assert response.status_code == 200
    payload = response.json()

    balances = payload["portfolio"]["balances"]
    assert any(balance["asset"] == "USDT" and balance["total"] == pytest.approx(1000.0) for balance in balances)
    exposures = payload["exposures"]
    assert any(entry["symbol"].upper() == "BTCUSDT" for entry in exposures)
    assert any(entry.get("venue_type") == "binance-testnet" for entry in exposures)


def test_kill_switch_cancels_orders(client, monkeypatch, tmp_path):
    secrets_payload = {
        "operator_tokens": {"ops": {"token": "OPS", "role": "operator"}},
        "approve_token": "ZZZ",
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("APPROVE_TOKEN", "ZZZ")
    reset_secrets_store_cache()

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
    headers = {"Authorization": "Bearer OPS"}
    kill_request = client.post(
        "/api/ui/kill-request",
        headers=headers,
        json={"reason": "test", "requested_by": "ops"},
    )
    assert kill_request.status_code == 202
    request_id = kill_request.json()["request_id"]

    async def _fake_cancel_all_orders(venue=None, *, correlation_id=None):
        return {"orders_cancelled": True, "order_ids": [order_id], "correlation_id": correlation_id}

    monkeypatch.setattr("app.routers.ui.cancel_all_orders", _fake_cancel_all_orders)

    resp = client.post(
        "/api/ui/kill",
        headers=headers,
        json={"request_id": request_id, "token": "ZZZ"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["safe_mode"] is True
    assert payload["mode"] == "HOLD"
    assert payload["request_id"] == request_id
    reset_secrets_store_cache()


def test_patch_control_rejects_in_live_or_unsafe(client):
    state = get_state()
    state.control.environment = "live"
    state.control.safe_mode = True
    resp = client.patch("/api/ui/control", json={"order_notional_usdt": 75.0})
    assert resp.status_code == 403

    state.control.environment = "paper"
    state.control.safe_mode = False
    resp = client.patch("/api/ui/control", json={"order_notional_usdt": 80.0})
    assert resp.status_code == 403
    state.control.safe_mode = True


def test_patch_control_applies_and_reflected_in_state(client):
    state = get_state()
    state.control.environment = "paper"
    state.control.safe_mode = True
    state.control.order_notional_usdt = 50.0
    state.control.min_spread_bps = 0.0
    state.control.max_slippage_bps = 2
    state.control.loop_venues = []
    state.control.dry_run = False
    state.control.loop_pair = "BTCUSDT"
    payload = {
        "order_notional_usdt": 123.45,
        "min_spread_bps": 1.2,
        "max_slippage_bps": 7,
        "loop_pair": "ethusdt",
        "loop_venues": ["binance-um", "okx-perp"],
        "dry_run_only": True,
        "safe_mode": True,
    }
    resp = client.patch("/api/ui/control", json=payload)
    assert resp.status_code == 200
    result = resp.json()
    assert result["control"]["order_notional_usdt"] == pytest.approx(123.45)
    assert result["control"]["loop_pair"] == "ETHUSDT"
    assert result["control"]["dry_run"] is True
    assert set(result["changes"].keys()) >= {"order_notional_usdt", "loop_pair", "loop_venues", "dry_run_only", "min_spread_bps", "max_slippage_bps"}
    runtime_state = get_state()
    assert runtime_state.control.order_notional_usdt == pytest.approx(123.45)
    assert runtime_state.control.loop_pair == "ETHUSDT"
    assert runtime_state.control.min_spread_bps == pytest.approx(1.2)
    assert runtime_state.control.max_slippage_bps == 7
    assert runtime_state.loop_config.pair == "ETHUSDT"
    assert runtime_state.loop_config.venues == ["binance-um", "okx-perp"]


def test_positions_close_exposure_endpoint_called_from_ui(client):
    class DummyRuntime:
        def __init__(self) -> None:
            self.calls = 0

        def flatten_all(self):
            self.calls += 1
            return {"results": []}

    state = get_state()
    state.derivatives = DummyRuntime()
    resp = client.post("/api/ui/close_exposure", json={"venue": "binance-um", "symbol": "BTCUSDT"})
    assert resp.status_code == 200
    assert state.derivatives.calls == 1


def test_events_levels_and_filtering(client):
    ledger.reset()
    ledger.record_event(level="INFO", code="info_check", payload={})
    ledger.record_event(level="WARNING", code="risk_block", payload={"reason": "risk:limit"})
    ledger.record_event(level="ERROR", code="execution_failed", payload={"order": 1})
    resp = client.get("/api/ui/state")
    assert resp.status_code == 200
    page = resp.json()["events"]
    levels = {entry["level"] for entry in page["items"]}
    assert {"INFO", "WARNING", "ERROR"} <= levels


def test_ui_control_patch_endpoint_persistence(client, monkeypatch, tmp_path):
    runtime_path = tmp_path / "runtime_state.json"
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(runtime_path))
    runtime.reset_for_tests()

    patch_payload = {
        "order_notional_usdt": 1500,
        "max_slippage_bps": 5,
        "min_spread_bps": 12,
    }

    resp = client.patch("/api/ui/control", json=patch_payload)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["control"]["order_notional_usdt"] == pytest.approx(1500)
    assert payload["control"]["max_slippage_bps"] == 5
    assert payload["control"]["min_spread_bps"] == pytest.approx(12)
    assert runtime_path.exists()

    runtime.reset_for_tests()
    state = get_state()
    assert state.control.order_notional_usdt == pytest.approx(1500)
    assert state.control.max_slippage_bps == 5
    assert state.control.min_spread_bps == pytest.approx(12)


def test_ui_control_patch_endpoint_validation(client):
    resp = client.patch("/api/ui/control", json={"max_slippage_bps": 100})
    assert resp.status_code == 422

    resp = client.patch("/api/ui/control", json={"order_notional_usdt": 0.5})
    assert resp.status_code == 422

    resp = client.patch("/api/ui/control", json={"order_notional_usdt": None})
    assert resp.status_code == 200
    assert resp.json()["changes"] == {}


def test_ui_events_endpoint_pagination(client):
    ledger.reset()
    for idx in range(5):
        ledger.record_event(level="INFO", code=f"evt_{idx}", payload={"idx": idx})

    resp = client.get("/api/ui/events", params={"limit": 2})
    assert resp.status_code == 200
    first_page = resp.json()
    assert first_page["limit"] == 2
    assert first_page["offset"] == 0
    assert first_page["total"] == 5
    assert first_page["has_more"] is True
    assert len(first_page["items"]) == 2
    assert "message" in first_page["items"][0]
    assert "type" in first_page["items"][0]

    resp_next = client.get("/api/ui/events", params={"offset": first_page["next_offset"], "limit": 2})
    assert resp_next.status_code == 200
    next_page = resp_next.json()
    assert next_page["offset"] == first_page["next_offset"]
    assert next_page["total"] == first_page["total"]
    assert len(next_page["items"]) <= 2


def test_risk_state_endpoint(client):
    ledger.reset()
    resp = client.get("/api/risk/state")
    assert resp.status_code == 200
    payload = resp.json()
    assert set(payload.keys()) >= {"limits", "current", "breaches", "positions_usdt", "exposures"}


def test_daily_loss_status_endpoint(client, monkeypatch):
    monkeypatch.setenv("DAILY_LOSS_CAP_USDT", "75")
    monkeypatch.setenv("ENFORCE_DAILY_LOSS_CAP", "1")
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()

    risk_accounting.record_fill("ui_test", 0.0, -30.0, simulated=False)

    resp = client.get("/api/ui/daily_loss_status")
    assert resp.status_code == 200
    snapshot = resp.json()
    assert snapshot["max_daily_loss_usdt"] == pytest.approx(75.0)
    assert snapshot["losses_usdt"] == pytest.approx(30.0)
    assert snapshot["breached"] is False
    assert snapshot["enabled"] is True
    assert snapshot["blocking"] is True

    state_resp = client.get("/api/ui/state")
    assert state_resp.status_code == 200
    state_payload = state_resp.json()
    embedded = state_payload.get("daily_loss_cap")
    assert embedded
    assert embedded["losses_usdt"] == pytest.approx(30.0)
