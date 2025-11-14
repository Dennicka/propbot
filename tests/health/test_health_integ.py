from __future__ import annotations

import time

from app.health import aggregator as health_aggregator
from app.health.watchdog import get_watchdog
from app.routers import health as health_router
from app.services import health_state as health_state_module
from app.services import live_readiness as live_readiness_module
from app.services.runtime import get_runtime_profile_snapshot


def _reset_watchdog_state() -> None:
    watchdog = get_watchdog()
    watchdog._router_last_activity = None
    watchdog._recon_last_run = None
    watchdog._ledger_last_update = None
    watchdog._marketdata_last_tick = None


def _stub_health(monkeypatch, **fields) -> None:
    payload = {
        "ok": True,
        "journal_ok": True,
        "resume_ok": True,
        "leader": True,
        "config_ok": True,
    }
    payload.update(fields)
    monkeypatch.setattr(health_router, "evaluate_health", lambda _app: payload)
    monkeypatch.setattr(health_state_module, "evaluate_health", lambda _app: payload)
    monkeypatch.setattr(live_readiness_module, "evaluate_health", lambda _app: payload)
    health_aggregator.ensure_watchdog_integration()


def test_health_endpoint_includes_watchdog_block(client, monkeypatch) -> None:
    _reset_watchdog_state()
    _stub_health(monkeypatch)
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    watchdog_block = payload.get("watchdog")
    assert isinstance(watchdog_block, dict)
    components = watchdog_block.get("components")
    assert isinstance(components, dict)
    for name in ("router", "recon", "ledger", "marketdata"):
        assert name in components


def test_health_watchdog_snapshot_ok_when_recent_activity(client, monkeypatch) -> None:
    _reset_watchdog_state()
    _stub_health(monkeypatch)
    watchdog = get_watchdog()
    now = time.time()
    watchdog.mark_router_activity(ts=now)
    watchdog.mark_recon_run(ts=now)
    watchdog.mark_ledger_update(ts=now)
    watchdog.mark_marketdata_tick(ts=now)

    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    watchdog_block = payload.get("watchdog")
    assert isinstance(watchdog_block, dict)
    assert watchdog_block.get("overall") == "ok"
    components = watchdog_block.get("components") or {}
    assert all(entry.get("level") != "fail" for entry in components.values())


def test_watchdog_influences_readiness_in_strict_profile(client, monkeypatch) -> None:
    _reset_watchdog_state()
    _stub_health(monkeypatch)
    watchdog = get_watchdog()
    now = time.time()
    watchdog.mark_router_activity(ts=now - 30.0)
    watchdog.mark_recon_run(ts=now)
    watchdog.mark_ledger_update(ts=now)
    watchdog.mark_marketdata_tick(ts=now)

    runtime_profile = get_runtime_profile_snapshot()
    strict_name = str(runtime_profile.get("name") or "live")
    monkeypatch.setenv("HEALTH_PROFILE_STRICT", strict_name)
    monkeypatch.setenv("HEALTH_FAIL_ON_WARN", "1")
    base_ready = {
        "ready": True,
        "reasons": [],
        "leader": True,
        "health_ok": True,
        "journal_ok": True,
        "config_ok": True,
        "fencing_id": None,
        "hb_age_sec": None,
    }
    monkeypatch.setattr(
        health_aggregator,
        "_ORIGINAL_COMPUTE_READINESS",
        lambda _app: dict(base_ready),
    )

    response = client.get("/live-readiness")
    assert response.status_code == 503
    payload = response.json()
    assert payload.get("ready") is False
    reasons = payload.get("reasons") or []
    assert any(reason == "health-watchdog-fail" for reason in reasons)
    watchdog_block = payload.get("watchdog")
    assert isinstance(watchdog_block, dict)
    assert watchdog_block.get("overall") in {"warn", "fail"}
