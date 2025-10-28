from __future__ import annotations

from positions import create_position

from app.services import approvals_store, runtime


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
    state.safety.counters.orders_placed_last_min = 3
    state.safety.counters.cancels_last_min = 1
    state.safety.limits.max_orders_per_min = 10
    state.safety.limits.max_cancels_per_min = 20

    create_position(
        symbol="ETHUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=1000.0,
        entry_spread_bps=12.0,
        leverage=2.0,
        entry_long_price=1800.0,
        entry_short_price=1805.0,
    )

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
    # Health section should name the monitored daemons
    assert "auto_hedge_daemon" in html
    assert "scanner" in html
