from __future__ import annotations

import types
from typing import Any

import pytest


class DummyRouter:
    def __init__(self) -> None:
        self._state = types.SimpleNamespace(
            control=types.SimpleNamespace(mode="RESUME", safe_mode=False, deployment_mode="paper"),
            risk=types.SimpleNamespace(
                limits=types.SimpleNamespace(
                    max_position_usdt={"binance": 1000.0, "okx": 500.0},
                    max_daily_loss_usdt=500.0,
                ),
                current=types.SimpleNamespace(
                    position_usdt={"binance": 250.0, "okx": 125.0},
                    daily_loss_usdt=-150.0,
                ),
            ),
        )
        self._profile = types.SimpleNamespace(name="paper")

    def get_state(self) -> Any:
        return self._state

    def get_profile(self) -> Any:
        return self._profile


class DummyRiskGovernor:
    def snapshot(self) -> dict[str, Any]:
        return {"throttled": False}


class DummyReadinessRegistry:
    def __init__(self) -> None:
        self.items = {"router": types.SimpleNamespace(ts=123.0)}

    def report(self) -> tuple[str, dict[str, dict[str, Any]]]:
        return "ready", {"router": {"stale": False, "reason": "ok"}}


class DummyMarketWatchdog:
    def report(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {"binance": {"BTCUSDT": {"stale": False}}}


class DummyAlertsRegistry:
    def last(self, limit: int | None = None) -> list[dict[str, Any]]:
        return [
            {"ts": 112.0, "level": "INFO", "message": "first"},
            {"ts": 111.0, "level": "WARN", "message": "second", "meta": {"code": 2}},
        ]


@pytest.mark.usefixtures("client")
def test_ui_status_snapshot_endpoint(client, monkeypatch):
    dummy_router = DummyRouter()
    dummy_risk = DummyRiskGovernor()
    dummy_readiness = DummyReadinessRegistry()
    dummy_watchdog = DummyMarketWatchdog()
    dummy_alerts = DummyAlertsRegistry()
    config_snapshot = {
        "runtime": {"name": "paper", "is_live": False},
        "router": {
            "mode": "RESUME",
            "safe_mode": False,
            "pretrade_strict": True,
            "risk_limits_enabled": True,
        },
        "risk_limits": {"enabled": True},
        "strategies": {"items": []},
    }

    monkeypatch.setattr("app.server_ws.runtime", dummy_router)
    monkeypatch.setattr("app.server_ws.get_risk_governor", lambda: dummy_risk)
    monkeypatch.setattr("app.server_ws.registry", dummy_readiness)
    monkeypatch.setattr("app.server_ws.market_watchdog", dummy_watchdog)
    monkeypatch.setattr("app.server_ws.alerts_registry", dummy_alerts)
    monkeypatch.setattr("app.server_ws.build_ui_config_snapshot", lambda: config_snapshot)

    monkeypatch.setattr("app.main.runtime_service.get_state", lambda: dummy_router.get_state())
    monkeypatch.setattr("app.main.runtime_service.get_profile", lambda: dummy_router.get_profile())
    monkeypatch.setattr("app.main.get_risk_governor", lambda: dummy_risk)
    monkeypatch.setattr("app.main.readiness_registry", dummy_readiness)
    monkeypatch.setattr("app.main.market_watchdog", dummy_watchdog)
    monkeypatch.setattr("app.main.alerts_registry", dummy_alerts)
    monkeypatch.setattr("app.main.build_ui_config_snapshot", lambda: config_snapshot)

    response = client.get("/api/ui/status")
    assert response.status_code == 200
    payload = response.json()

    assert payload["router"]["mode"] == "RESUME"
    assert payload["router"]["safe_mode"] is False
    assert payload["router"]["profile"] == "paper"
    assert payload["router"]["ff_pretrade_strict"] is True
    assert payload["router"]["ff_risk_limits"] is True

    risk = payload["risk"]
    assert risk["daily_loss_cap"] == pytest.approx(500.0)
    assert risk["daily_loss_pnl"] == pytest.approx(-150.0)
    assert risk["daily_loss_remaining"] == pytest.approx(350.0)
    assert risk["notional_caps"]["binance"] == pytest.approx(1000.0)
    assert risk["notional_used"]["binance"] == pytest.approx(250.0)
    assert risk["notional_remaining"]["binance"] == pytest.approx(750.0)

    readiness = payload["readiness"]
    assert readiness["live_ready"] is True
    assert readiness["last_reason"] == "ok"
    assert readiness["last_check_ts"] == pytest.approx(123.0)

    market_data = payload["market_data"]
    assert market_data["healthy"] is True
    assert market_data["stale_symbols"] == []

    alerts = payload["alerts"]
    assert isinstance(alerts["last_n"], list)
    assert alerts["last_n"][0]["message"] == "first"
    assert alerts["last_n"][1]["message"] == "second"

    config = payload["config"]
    assert config == config_snapshot
    assert "runtime" in config
    assert "router" in config
    assert "risk_limits" in config
    assert "strategies" in config

    runtime = config["runtime"]
    assert isinstance(runtime.get("name"), str)
    assert isinstance(runtime.get("is_live"), bool)

    router = config["router"]
    assert "safe_mode" in router

    risk_limits = config["risk_limits"]
    assert "enabled" in risk_limits

    strategies_block = config["strategies"]
    assert "items" in strategies_block
    assert isinstance(strategies_block["items"], list)
