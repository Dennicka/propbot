from __future__ import annotations

import types

import pytest

from app import main as app_main
from app.readiness import aggregator as readiness_module
from app.services import runtime


def _ctx(**overrides):
    base = {
        "config_loaded": True,
        "db_ok": True,
        "metrics_ok": True,
        "md_connected": True,
        "md_staleness_ok": True,
        "watchdog_state": "OK",
        "recon_divergence_ok": True,
        "pretrade_throttled": False,
        "risk_throttled": False,
        "router_ready": True,
        "state": "RUN",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_startup_waits_until_green_then_runs(monkeypatch):
    monkeypatch.setenv("SAFE_MODE", "false")
    monkeypatch.setenv("ENVIRONMENT", "live")
    monkeypatch.setenv("WAIT_FOR_LIVE_READINESS_ON_START", "true")

    monkeypatch.setattr(
        app_main.runtime_service, "setup_signal_handlers", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(app_main, "validate_startup", lambda: None)
    monkeypatch.setattr(app_main, "perform_startup_resume", lambda: (True, {}))
    monkeypatch.setattr(app_main, "setup_telegram_bot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_main, "setup_opportunity_scanner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_main, "setup_auto_hedge_daemon", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_main, "setup_autopilot", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_main, "setup_autopilot_guard", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_main, "setup_orchestrator_alerts", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_main, "setup_exchange_watchdog", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_main, "setup_recon_runner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_main, "setup_partial_hedge_runner", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app_main, "setup_slo_monitor", lambda *_args, **_kwargs: None)

    control = types.SimpleNamespace(
        mode="RUN",
        safe_mode=False,
        environment="live",
        deployment_mode="live",
    )
    state = types.SimpleNamespace(control=control)

    monkeypatch.setattr(app_main.runtime_service, "get_state", lambda: state)
    monkeypatch.setattr(runtime, "get_state", lambda: state)
    monkeypatch.setattr(app_main, "_startup_timeout_seconds", lambda _state: 0.1)

    snapshots = [
        _ctx(pretrade_throttled=True),
        _ctx(),
    ]
    calls = {"count": 0}

    def fake_collect():
        index = calls["count"]
        calls["count"] = min(index + 1, len(snapshots) - 1)
        return snapshots[index]

    aggregator_instance = readiness_module.LiveReadinessAggregator()
    monkeypatch.setattr(readiness_module, "collect_readiness_signals", fake_collect)
    monkeypatch.setattr(readiness_module, "READINESS_AGGREGATOR", aggregator_instance)
    monkeypatch.setattr(app_main, "collect_readiness_signals", fake_collect)
    monkeypatch.setattr(app_main, "READINESS_AGGREGATOR", aggregator_instance)
    monkeypatch.setattr(app_main, "READINESS_POLL_INTERVAL", 0.01)

    resume_calls: list[bool] = []
    hold_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_autopilot_apply_resume(*, safe_mode: bool):
        resume_calls.append(safe_mode)
        control.mode = "RUN"
        return {"hold_cleared": True}

    def fake_engage_safety_hold(*args, **kwargs):
        hold_calls.append((args, kwargs))
        control.mode = "HOLD"
        return True

    monkeypatch.setattr(
        app_main.runtime_service, "autopilot_apply_resume", fake_autopilot_apply_resume
    )
    monkeypatch.setattr(runtime, "autopilot_apply_resume", fake_autopilot_apply_resume)
    monkeypatch.setattr(app_main.runtime_service, "engage_safety_hold", fake_engage_safety_hold)
    monkeypatch.setattr(runtime, "engage_safety_hold", fake_engage_safety_hold)

    app = app_main.create_app()

    await app.router.startup()

    assert resume_calls == [False]
    assert hold_calls
    assert runtime.get_state().control.mode == "RUN"
    assert calls["count"] >= 1

    await app.router.shutdown()
