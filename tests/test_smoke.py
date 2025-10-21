from __future__ import annotations

from typing import Iterable

from app.exchanges import binance_um, okx_perp


def _assert_all_ok(client, endpoints: Iterable[str]) -> None:
    for endpoint in endpoints:
        response = client.get(endpoint)
        assert response.status_code == 200, endpoint


def test_smoke_endpoints(client, monkeypatch) -> None:
    get_endpoints = [
        "/api/health",
        "/openapi.json",
        "/metrics",
        "/metrics/latency",
        "/live-readiness",
        "/api/opportunities",
        "/api/ui/status/overview",
        "/api/ui/status/components",
        "/api/ui/status/slo",
        "/api/ui/control-state",
        "/api/ui/state",
        "/api/ui/execution",
        "/api/ui/pnl",
        "/api/ui/exposure",
        "/api/ui/limits",
        "/api/ui/universe",
        "/api/ui/approvals",
        "/api/ui/recon/status",
        "/api/ui/recon/history",
        "/api/deriv/status",
        "/api/deriv/positions",
        "/api/arb/edge",
    ]
    _assert_all_ok(client, get_endpoints)

    ui_state = client.get("/api/ui/state").json()
    assert "flags" in ui_state
    for key in [
        "MODE",
        "SAFE_MODE",
        "POST_ONLY",
        "REDUCE_ONLY",
        "ENV",
        "DRY_RUN",
        "ORDER_NOTIONAL_USDT",
        "MAX_SLIPPAGE_BPS",
    ]:
        assert key in ui_state["flags"]

    monkeypatch.setattr(
        binance_um,
        "get_book",
        lambda symbol: {"bid": 20150.0, "ask": 20160.0, "ts": 1},
    )
    monkeypatch.setattr(
        okx_perp,
        "get_book",
        lambda symbol: {"bid": 20180.0, "ask": 20190.0, "ts": 1},
    )

    preview = client.get(
        "/api/arb/preview",
        params={"symbol": "BTCUSDT", "notional": 50, "slippage_bps": 2},
    )
    assert preview.status_code == 200
    preview_payload = preview.json()
    assert preview_payload["symbol"] == "BTCUSDT"
    assert "viable" in preview_payload

    recon_run = client.post("/api/ui/recon/run")
    assert recon_run.status_code == 200
    assert recon_run.json()["ok"] is True
