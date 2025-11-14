from __future__ import annotations

from app.health.watchdog import HealthWatchdog, get_watchdog


def test_snapshot_never_seen_warn():
    watchdog = get_watchdog()
    watchdog._router_last_activity = None  # reset singleton state
    watchdog._recon_last_run = None
    watchdog._ledger_last_update = None
    watchdog._marketdata_last_tick = None

    snap = watchdog.snapshot(now=100.0)

    assert snap.overall == "warn"
    router = snap.components["router"]
    recon = snap.components["recon"]
    ledger = snap.components["ledger"]

    assert router.level == "warn"
    assert router.reason == "never-seen"
    assert router.last_ts is None

    assert recon.level == "warn"
    assert recon.reason == "never-seen"
    assert recon.last_ts is None

    assert ledger.level == "warn"
    assert ledger.reason == "never-seen"
    assert ledger.last_ts is None


def test_snapshot_all_ok_levels():
    watchdog = HealthWatchdog()
    now = 1000.0
    watchdog.mark_router_activity(ts=now)
    watchdog.mark_recon_run(ts=now)
    watchdog.mark_ledger_update(ts=now)
    watchdog.mark_marketdata_tick(ts=now)

    snap = watchdog.snapshot(now=now + 1)

    assert snap.overall == "ok"
    for component in snap.components.values():
        assert component.level == "ok"
        assert component.reason == ""
        assert component.last_ts == now


def test_snapshot_timeout_failure(monkeypatch):
    monkeypatch.setenv("HEALTH_MAX_ROUTER_IDLE_SEC", "5")
    watchdog = HealthWatchdog()
    watchdog.mark_router_activity(ts=0.0)

    snap = watchdog.snapshot(now=20.0)

    router = snap.components["router"]
    assert router.level == "fail"
    assert router.reason == "timeout"
    assert snap.overall == "fail"
