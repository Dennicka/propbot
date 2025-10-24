from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.services import runtime


class _DummyPlan:
    viable = True
    reason = None


class _DummyReport:
    def as_dict(self) -> dict:
        return {}


def _auth_headers(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _prepare_mutation_stubs(monkeypatch) -> runtime.RuntimeState:
    state = runtime.get_state()
    state.control.environment = "testnet"

    monkeypatch.setattr(
        "app.routers.ui.cancel_all_orders",
        AsyncMock(return_value={"cancelled": 1, "failed": 0}),
    )

    def _fake_control_patch(payload):
        return state.control, payload or {}

    monkeypatch.setattr("app.routers.ui.apply_control_patch", _fake_control_patch)

    monkeypatch.setattr(
        "app.routers.arb.arbitrage.plan_from_payload",
        lambda payload: _DummyPlan(),
    )
    monkeypatch.setattr(
        "app.routers.arb.arbitrage.execute_plan_async",
        AsyncMock(return_value=_DummyReport()),
    )

    return state


def test_mutations_open_when_auth_disabled(monkeypatch, client: TestClient) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    state = _prepare_mutation_stubs(monkeypatch)

    cancel_resp = client.post("/api/ui/cancel_all", json={})
    assert cancel_resp.status_code == 200

    control_resp = client.patch("/api/ui/control", json={})
    assert control_resp.status_code == 200

    state.control.safe_mode = False
    execute_payload = {
        "symbol": "BTCUSDT",
        "notional": 1000,
        "viable": True,
        "legs": [],
        "est_pnl_usdt": 0.0,
        "est_pnl_bps": 0.0,
        "used_fees_bps": {},
        "used_slippage_bps": 0,
    }
    execute_resp = client.post("/api/arb/execute", json=execute_payload)
    assert execute_resp.status_code == 200


def test_mutations_require_token_when_enabled(monkeypatch, client: TestClient) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")
    state = _prepare_mutation_stubs(monkeypatch)

    cancel_missing = client.post("/api/ui/cancel_all", json={})
    assert cancel_missing.status_code == 401

    cancel_wrong = client.post(
        "/api/ui/cancel_all",
        json={},
        headers=_auth_headers("bad-token"),
    )
    assert cancel_wrong.status_code == 401

    cancel_ok = client.post(
        "/api/ui/cancel_all",
        json={},
        headers=_auth_headers("secret-token"),
    )
    assert cancel_ok.status_code == 200

    control_missing = client.patch("/api/ui/control", json={})
    assert control_missing.status_code == 401

    control_wrong = client.patch(
        "/api/ui/control",
        json={},
        headers=_auth_headers("bad-token"),
    )
    assert control_wrong.status_code == 401

    control_ok = client.patch(
        "/api/ui/control",
        json={},
        headers=_auth_headers("secret-token"),
    )
    assert control_ok.status_code == 200

    state.control.safe_mode = False
    execute_payload = {
        "symbol": "BTCUSDT",
        "notional": 1000,
        "viable": True,
        "legs": [],
        "est_pnl_usdt": 0.0,
        "est_pnl_bps": 0.0,
        "used_fees_bps": {},
        "used_slippage_bps": 0,
    }

    execute_missing = client.post("/api/arb/execute", json=execute_payload)
    assert execute_missing.status_code == 401

    execute_wrong = client.post(
        "/api/arb/execute",
        json=execute_payload,
        headers=_auth_headers("bad-token"),
    )
    assert execute_wrong.status_code == 401

    execute_ok = client.post(
        "/api/arb/execute",
        json=execute_payload,
        headers=_auth_headers("secret-token"),
    )
    assert execute_ok.status_code == 200


def test_reads_remain_public(monkeypatch, client: TestClient) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    response = client.get("/api/ui/state")
    assert response.status_code == 200

    monkeypatch.setenv("AUTH_ENABLED", "false")
    response_disabled = client.get("/api/ui/state")
    assert response_disabled.status_code == 200
