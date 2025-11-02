from __future__ import annotations

from app.readiness.aggregator import LiveReadinessAggregator, ReadinessStatus


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


def test_readiness_green_when_all_ok():
    aggregator = LiveReadinessAggregator()
    snapshot = aggregator.snapshot(_ctx())

    assert snapshot["status"] == ReadinessStatus.GREEN.value
    assert snapshot["reasons"] == []


def test_readiness_red_when_pretrade_throttled():
    aggregator = LiveReadinessAggregator()
    snapshot = aggregator.snapshot(_ctx(pretrade_throttled=True))

    assert snapshot["status"] == ReadinessStatus.RED.value
    assert "pretrade_throttled" in snapshot["reasons"]


def test_readiness_red_on_md_staleness_or_down():
    aggregator = LiveReadinessAggregator()
    stale = aggregator.snapshot(_ctx(md_staleness_ok=False))
    assert stale["status"] == ReadinessStatus.RED.value
    assert "md_staleness" in stale["reasons"]

    disconnected = aggregator.snapshot(_ctx(md_connected=False))
    assert disconnected["status"] == ReadinessStatus.RED.value
    assert "md_disconnected" in disconnected["reasons"]


def test_readiness_yellow_on_degraded_or_manual_hold():
    aggregator = LiveReadinessAggregator()
    degraded = aggregator.snapshot(_ctx(watchdog_state="degraded"))
    assert degraded["status"] == ReadinessStatus.YELLOW.value
    assert "watchdog_degraded" in degraded["reasons"]

    manual = aggregator.snapshot(_ctx(state="hold"))
    assert manual["status"] == ReadinessStatus.YELLOW.value
    assert "manual_hold" in manual["reasons"]
