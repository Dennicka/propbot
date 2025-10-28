from __future__ import annotations

from positions import create_position

import asyncio

from app.services import approvals_store, risk_guard, runtime
from app.services.pnl_history import record_snapshot
from app.services.runtime import is_hold_active


def test_dashboard_requires_token(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "super-secret")

    response = client.get("/ui/dashboard")
    assert response.status_code in {401, 403}


def test_dashboard_renders_runtime_snapshot(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "dashboard-token")

    runtime.engage_safety_hold("pytest", source="test")
    runtime.update_auto_hedge_state(
        enabled=True,
        last_success_ts="2024-01-01T00:00:00+00:00",
        last_execution_result="ok",
        consecutive_failures=2,
    )
    state = runtime.get_state()
    state.safety.counters.orders_placed_last_min = 9
    state.safety.counters.cancels_last_min = 8
    state.safety.limits.max_orders_per_min = 10
    state.safety.limits.max_cancels_per_min = 10

    create_position(
        symbol="ETHUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=1000.0,
        entry_spread_bps=12.0,
        leverage=2.0,
        entry_long_price=1800.0,
        entry_short_price=1805.0,
        status="partial",
        legs=[
            {
                "side": "long",
                "venue": "binance-um",
                "symbol": "ETHUSDT",
                "notional_usdt": 1000.0,
                "status": "partial",
            },
            {
                "side": "short",
                "venue": "okx-perp",
                "symbol": "ETHUSDT",
                "notional_usdt": 500.0,
                "status": "partial",
            },
        ],
    )

    create_position(
        symbol="BTCUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=500.0,
        entry_spread_bps=5.0,
        leverage=1.5,
        entry_long_price=28000.0,
        entry_short_price=28010.0,
        simulated=True,
        status="open",
    )

    class DummySnapshot:
        pnl_totals = {"unrealized": 25.0, "total": 25.0, "realized": 0.0}

    async def fake_snapshot(*_args, **_kwargs):
        return DummySnapshot()

    monkeypatch.setattr("app.services.pnl_history.portfolio.snapshot", fake_snapshot)
    asyncio.run(record_snapshot(reason="test"))

    approvals_store.create_request(
        "resume",
        requested_by="alice",
        parameters={"reason": "go-live"},
    )

    response = client.get(
        "/ui/dashboard",
        headers={"Authorization": "Bearer dashboard-token"},
    )
    assert response.status_code == 200
    html = response.text
    assert "Operator Dashboard" in html
    assert "Build Version" in html
    assert "HOLD Active" in html
    assert "Auto-Hedge" in html
    assert "ETHUSDT" in html
    assert "binance-um" in html
    assert "Pending Approvals" in html
    assert "Controls" in html
    assert "Request RESUME" in html
    assert "Emergency CANCEL ALL" in html
    assert "OUTSTANDING RISK" in html
    assert "SIMULATED" in html
    assert "NEAR LIMIT" in html
    # Health section should name the monitored daemons
    assert "auto_hedge_daemon" in html
    assert "scanner" in html

    assert "Pending Approvals" in html
    assert "resume" in html
    assert "alice" in html
    assert "reason" in html

    assert "form method=\"post\" action=\"/ui/dashboard/hold\"" in html
    assert "form method=\"post\" action=\"/ui/dashboard/resume\"" in html
    assert "form method=\"post\" action=\"/ui/dashboard/kill\"" in html

    assert "Risk &amp; PnL trend" in html
    assert "Unrealised PnL" in html
    assert "Total Exposure (USD)" in html
    assert "Open positions:" in html


def test_dashboard_shows_risk_throttle_banner(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "risk-throttle")

    runtime.engage_safety_hold(risk_guard.REASON_PARTIAL_STALLED, source="risk_guard")

    response = client.get(
        "/ui/dashboard",
        headers={"Authorization": "Bearer risk-throttle"},
    )

    assert response.status_code == 200
    html = response.text
    assert "RISK_THROTTLED" in html
    assert "Manual two-step RESUME approval required" in html


def test_dashboard_proxy_routes(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "dashboard-token")

    unauth = client.post("/ui/dashboard/hold", data={"reason": "panic"})
    assert unauth.status_code in {401, 403}

    headers = {"Authorization": "Bearer dashboard-token"}

    hold_response = client.post(
        "/ui/dashboard/hold",
        headers=headers,
        data={"reason": "panic", "operator": "alice"},
    )
    assert hold_response.status_code == 200
    assert "HOLD engaged" in hold_response.text
    assert is_hold_active()

    resume_response = client.post(
        "/ui/dashboard/resume",
        headers=headers,
        data={"reason": "ready", "operator": "bob"},
    )
    assert resume_response.status_code == 202
    assert "Resume request logged" in resume_response.text

    approvals = approvals_store.list_requests()
    pending = [entry for entry in approvals if entry.get("status") == "pending"]
    assert pending
    assert pending[0]["action"] == "resume"
    assert pending[0]["parameters"].get("reason") == "ready"
    assert is_hold_active()
