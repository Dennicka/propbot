from positions import create_position

from app.services.runtime import engage_safety_hold, get_state


def test_risk_snapshot_endpoint(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")

    state = get_state()
    state.autopilot.enabled = True
    state.control.dry_run_mode = False

    engage_safety_hold("test-hold", source="pytest")

    create_position(
        symbol="ETHUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=1000.0,
        entry_spread_bps=12.0,
        leverage=2.0,
        entry_long_price=1800.0,
        entry_short_price=1805.0,
        status="open",
    )

    create_position(
        symbol="BTCUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=500.0,
        entry_spread_bps=8.0,
        leverage=1.5,
        entry_long_price=25000.0,
        entry_short_price=25010.0,
        status="partial",
    )

    create_position(
        symbol="LTCUSDT",
        long_venue="bybit-perp",
        short_venue="okx-perp",
        notional_usdt=750.0,
        entry_spread_bps=5.0,
        leverage=1.0,
        entry_long_price=90.0,
        entry_short_price=90.5,
        status="open",
        simulated=True,
    )

    response = client.get(
        "/api/ui/risk_snapshot",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["autopilot_enabled"] is True
    assert payload["hold_active"] is True
    assert payload["safe_mode"] is True
    assert payload["dry_run_mode"] is False
    assert payload["risk_score"] == "TBD"
    assert payload["partial_hedges_count"] == 2

    total_notional = payload["total_notional_usd"]
    # two legs per open position: 2 * 1000 + 2 * 500
    assert total_notional == 3000.0

    venues = payload["per_venue"]
    assert "binance-um" in venues
    assert venues["binance-um"]["open_positions_count"] >= 1

    accounting = payload.get("accounting", {})
    per_strategy = accounting.get("per_strategy", {})
    for details in per_strategy.values():
        assert "blocked_by_budget" in details
        budget_info = details.get("budget", {})
        assert "limit_usdt" in budget_info
        assert "used_today_usdt" in budget_info
        assert "remaining_usdt" in budget_info
        assert "last_reset_ts_utc" in budget_info
    loss_cap = accounting.get("bot_loss_cap")
    assert isinstance(loss_cap, dict)
    assert "cap_usdt" in loss_cap
    assert "realized_today_usdt" in loss_cap
    assert "remaining_usdt" in loss_cap
    assert "breached" in loss_cap

    unauth_response = client.get("/api/ui/risk_snapshot")
    assert unauth_response.status_code in {401, 403}
