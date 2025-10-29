from __future__ import annotations

import asyncio
import json

from positions import create_position

from app.services import approvals_store, risk_guard, runtime
from app.services.pnl_history import record_snapshot
from app.services.runtime import is_hold_active
from app.version import APP_VERSION


def test_dashboard_requires_token(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "super-secret")

    response = client.get("/ui/dashboard")
    assert response.status_code in {401, 403}


def test_dashboard_strategy_orchestrator_plan(monkeypatch, tmp_path, client) -> None:
    class DummyOrchestrator:
        def compute_next_plan(self):
            return {
                "ts": "2024-01-02T03:04:05+00:00",
                "risk_gates": {"risk_caps_ok": False, "reason_if_blocked": "hold_active"},
                "strategies": [
                    {
                        "name": "cross_exchange_arb",
                        "decision": "skip",
                        "reason": "hold_active",
                        "last_error": "blocked",
                        "last_run_ts": "2024-01-01T00:00:00+00:00",
                    },
                    {
                        "name": "hedger",
                        "decision": "cooldown",
                        "reason": "recent_fail",
                        "last_result": "error",
                    },
                ],
            }

    monkeypatch.setattr(
        "app.services.operator_dashboard.strategy_orchestrator",
        DummyOrchestrator(),
    )

    secrets_payload = {
        "operator_tokens": {"viewer": {"token": "VVV", "role": "viewer"}},
        "approve_token": "ZZZ",
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")

    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)

    response = client.get(
        "/ui/dashboard",
        headers={"Authorization": "Bearer VVV"},
    )

    assert response.status_code == 200
    html = response.text
    assert "Strategy Orchestrator" in html
    assert "Orchestrator alerts for skip/risk_limit/hold_active and cooldown/fail are forwarded to ops Telegram/audit." in html
    assert "cross_exchange_arb" in html
    assert "decision-skip-critical\">skip" in html
    assert "hold_active" in html
    assert "decision-cooldown\">cooldown" in html
    assert "recent_fail" in html
    assert "strategy-orchestrator-readonly\">READ ONLY" in html


def test_dashboard_viewer_read_only(monkeypatch, tmp_path, client) -> None:
    secrets_payload = {
        "operator_tokens": {
            "alice": {"token": "AAA", "role": "operator"},
            "bob": {"token": "BBB", "role": "viewer"},
        },
        "approve_token": "ZZZ",
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")

    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)

    response = client.get(
        "/ui/dashboard",
        headers={"Authorization": "Bearer BBB"},
    )

    assert response.status_code == 200
    html = response.text
    assert "READ ONLY: you cannot change HOLD/RESUME/KILL." in html
    assert "role-badge role-viewer" in html
    assert html.count("button type=\"submit\"") >= 3
    assert "button type=\"submit\" disabled" in html


def test_dashboard_pnl_snapshot_visible(monkeypatch, tmp_path, client) -> None:
    monkeypatch.setattr(
        "app.services.operator_dashboard.build_pnl_snapshot",
        lambda *_args, **_kwargs: {
            "unrealized_pnl_usdt": 123.45,
            "realised_pnl_today_usdt": 0.0,
            "total_exposure_usdt": 6789.0,
            "capital_headroom_per_strategy": {
                "cross_exchange_arb": {"headroom_notional": 1000.0}
            },
            "capital_snapshot": {
                "per_strategy_limits": {
                    "cross_exchange_arb": {"max_notional": 5000.0}
                },
                "current_usage": {
                    "cross_exchange_arb": {"open_notional": 4000.0}
                },
            },
        },
    )

    secrets_payload = {
        "operator_tokens": {"viewer": {"token": "VVV", "role": "viewer"}},
        "approve_token": "ZZZ",
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")

    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)

    response = client.get(
        "/ui/dashboard",
        headers={"Authorization": "Bearer VVV"},
    )

    assert response.status_code == 200
    html = response.text
    assert "PnL / Risk" in html
    assert "headroom" in html.lower()


def test_dashboard_risk_snapshot_viewer(monkeypatch, tmp_path, client) -> None:
    async def fake_risk_snapshot() -> dict[str, object]:
        return {
            "total_notional_usd": 12345.6,
            "per_venue": {
                "binance": {
                    "net_exposure_usd": 1000.0,
                    "unrealised_pnl_usd": -42.0,
                    "open_positions_count": 2,
                }
            },
            "partial_hedges_count": 3,
            "autopilot_enabled": False,
            "risk_score": "TBD",
        }

    monkeypatch.setattr(
        "app.services.operator_dashboard.build_risk_snapshot",
        fake_risk_snapshot,
    )

    secrets_payload = {
        "operator_tokens": {"viewer": {"token": "VVV", "role": "viewer"}},
        "approve_token": "ZZZ",
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")

    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("API_TOKEN", raising=False)

    response = client.get(
        "/ui/dashboard",
        headers={"Authorization": "Bearer VVV"},
    )

    assert response.status_code == 200
    html = response.text
    assert "Risk snapshot" in html
    assert "partial_hedges_count" in html
    assert "READ ONLY: you cannot change HOLD/RESUME/KILL." in html


def test_dashboard_renders_runtime_snapshot(monkeypatch, tmp_path, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "dashboard-token")

    secrets_payload = {
        "operator_tokens": {"alice": {"token": "dashboard-token", "role": "operator"}},
        "approve_token": "approver",
    }
    secrets_path = tmp_path / "secrets.json"
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")
    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))

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

    runtime.update_liquidity_snapshot(
        {
            "binance": {
                "free_usdt": 1250.0,
                "used_usdt": 250.0,
                "risk_ok": True,
                "reason": "ok",
            },
            "okx": {
                "free_usdt": 20.0,
                "used_usdt": 980.0,
                "risk_ok": False,
                "reason": "free balance below hedge size",
            },
        },
        blocked=True,
        reason="okx:free balance below hedge size",
        auto_hold=False,
    )
    runtime.update_reconciliation_status(
        desync_detected=True,
        issues=[
            {
                "kind": "position_missing_on_exchange",
                "venue": "okx-perp",
                "symbol": "ETHUSDT",
                "side": "short",
                "description": "leg missing",
            }
        ],
        last_checked="2024-01-01T00:00:00+00:00",
    )

    response = client.get(
        "/ui/dashboard",
        headers={"Authorization": "Bearer dashboard-token"},
    )
    assert response.status_code == 200
    html = response.text
    assert "Operator Dashboard" in html
    assert "Build Version" in html
    assert "Operator:" in html
    assert "Role:" in html
    assert "role-badge role-operator" in html
    assert "READ ONLY" not in html
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
    assert "Edge guard status" in html
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
    assert "button type=\"submit\" disabled" not in html

    assert "Risk &amp; PnL trend" in html
    assert "Unrealised PnL" in html
    assert "Total Exposure (USD)" in html
    assert "Open positions:" in html
    assert "Recent Ops / Incidents" in html

    assert "Balances / Liquidity" in html
    assert "TRADING HALTED FOR SAFETY" in html
    assert "okx" in html
    assert "free balance below hedge size" in html
    assert "Reconciliation status" in html
    assert "STATE DESYNC — manual intervention required" in html
    assert "Outstanding mismatches:" in html


def test_dashboard_recent_ops_status_badges(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "ops-ui")

    def fake_recent_events(limit: int = 10) -> list[dict[str, str]]:
        return [
            {
                "timestamp": "2024-05-01T00:00:00+00:00",
                "actor": "alice",
                "action": "Resume requested",
                "status": "pending",
                "reason": "Need to restart",
            },
            {
                "timestamp": "2024-05-01T00:01:00+00:00",
                "actor": "risk_guard",
                "action": "Auto-throttle HOLD",
                "status": "applied",
                "reason": "Risk breach",
            },
            {
                "timestamp": "2024-05-01T00:02:00+00:00",
                "actor": "bob",
                "action": "Resume approved",
                "status": "approved",
                "reason": "Cleared",
            },
        ][:limit]

    monkeypatch.setattr(
        "app.services.operator_dashboard.list_recent_events", fake_recent_events
    )

    response = client.get(
        "/ui/dashboard",
        headers={"Authorization": "Bearer ops-ui"},
    )
    assert response.status_code == 200
    html = response.text
    assert "Recent Ops / Incidents" in html
    assert "AUTO-HOLD" in html
    assert "PENDING" in html
    assert "APPROVED" in html or "APPLIED" in html
    assert "Need to restart" in html


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


def test_dashboard_renders_execution_quality(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "quality-token")

    sample_entries = [
        {
            "timestamp": "2024-05-01T00:00:00+00:00",
            "venue": "binance",
            "side": "long",
            "planned_px": 100.0,
            "real_fill_px": 100.3,
            "slippage_bps": 3.0,
            "success": False,
        },
        {
            "timestamp": "2024-05-01T00:01:00+00:00",
            "venue": "okx",
            "side": "short",
            "planned_px": 101.0,
            "real_fill_px": 101.2,
            "slippage_bps": -2.0,
            "success": True,
        },
    ]

    monkeypatch.setattr(
        "app.services.operator_dashboard.list_recent_execution_stats",
        lambda limit=15: sample_entries,
    )

    response = client.get(
        "/ui/dashboard",
        headers={"Authorization": "Bearer quality-token"},
    )

    assert response.status_code == 200
    html = response.text
    assert "Execution Quality" in html
    assert "Success rate" in html
    assert "Slippage (bps)" in html
    assert "binance" in html.lower()
    assert "okx" in html.lower()


def test_dashboard_footer_contains_build_and_warning(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "footer-token")

    timestamp = "2024-05-24T12:00:00+00:00"

    class DummySnapshot:
        pnl_totals = {"unrealized": 0.0}

    async def fake_snapshot(*_args, **_kwargs):
        return DummySnapshot()

    monkeypatch.setattr("app.services.pnl_history._ts", lambda: timestamp)
    monkeypatch.setattr("app.services.pnl_history.portfolio.snapshot", fake_snapshot)

    asyncio.run(record_snapshot(reason="footer-test"))

    response = client.get(
        "/ui/dashboard",
        headers={"Authorization": "Bearer footer-token"},
    )

    assert response.status_code == 200
    html = response.text
    assert f"Build version: <strong>{APP_VERSION}</strong>" in html
    assert timestamp in html
    assert "All trading actions require dual approval. Manual overrides are audited." in html


def test_dashboard_html_includes_daily_report_section() -> None:
    from app.services.operator_dashboard import render_dashboard_html

    context = {
        "build_version": "test",
        "safety": {},
        "auto_hedge": {},
        "risk_limits_env": {},
        "risk_limits_state": {},
        "positions": [],
        "exposure": {},
        "position_totals": {},
        "health_checks": [],
        "pending_approvals": [],
        "persisted_snapshot": {},
        "active_alerts": [],
        "recent_audit": [],
        "recent_ops_incidents": [],
        "risk_throttled": False,
        "risk_throttle_reason": "",
        "edge_guard": {"allowed": True, "reason": "ok", "context": {}},
        "pnl_history": [],
        "pnl_trend": {},
        "risk_advice": {},
        "execution_quality": {},
        "daily_report": {
            "timestamp": "2024-05-01T00:00:00+00:00",
            "window_hours": 24,
            "pnl_realized_total": 10.0,
            "pnl_unrealized_avg": 5.0,
            "exposure_avg": 1_000.0,
            "slippage_avg_bps": 0.75,
            "hold_events": 2,
            "hold_breakdown": {"safety_hold": 1, "risk_throttle": 1},
            "pnl_unrealized_samples": 3,
            "exposure_samples": 3,
            "slippage_samples": 2,
        },
    }

    html = render_dashboard_html(context)
    assert "Daily PnL / Ops summary" in html
    assert "PnL realised (24h)" in html
    assert "HOLD / throttle events" in html
