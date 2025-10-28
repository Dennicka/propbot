from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services import daily_reporter


def test_build_daily_report_aggregates_metrics() -> None:
    now = datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc)
    positions = [
        {"status": "closed", "closed_ts": (now - timedelta(hours=2)).isoformat(), "pnl_usdt": 50.0},
        {"status": "closed", "closed_ts": (now - timedelta(hours=3)).isoformat(), "pnl_usdt": -5.0},
        {"status": "closed", "closed_ts": (now - timedelta(hours=27)).isoformat(), "pnl_usdt": 42.0},
    ]
    snapshots = [
        {
            "timestamp": (now - timedelta(hours=1)).isoformat(),
            "pnl_totals": {"unrealized": 20.0},
            "total_exposure_usd_total": 1_000.0,
        },
        {
            "timestamp": (now - timedelta(hours=5)).isoformat(),
            "unrealized_pnl_total": 10.0,
            "total_exposure_usd_total": 500.0,
        },
        {
            "timestamp": (now - timedelta(hours=30)).isoformat(),
            "unrealized_pnl_total": 99.0,
            "total_exposure_usd_total": 999.0,
        },
    ]
    execution_stats = [
        {"timestamp": (now - timedelta(hours=2)).isoformat(), "slippage_bps": 1.2},
        {"timestamp": (now - timedelta(hours=10)).isoformat(), "slippage_bps": 0.5},
        {"timestamp": (now - timedelta(hours=28)).isoformat(), "slippage_bps": 3.0},
    ]
    alerts = [
        {"ts": (now - timedelta(hours=4)).isoformat(), "kind": "safety_hold"},
        {"ts": (now - timedelta(hours=6)).isoformat(), "kind": "risk_guard_force_hold"},
        {"ts": (now - timedelta(hours=40)).isoformat(), "kind": "safety_hold"},
    ]

    report = daily_reporter.build_daily_report(
        now=now,
        positions=positions,
        pnl_snapshots=snapshots,
        execution_stats=execution_stats,
        ops_alerts=alerts,
    )

    assert report["pnl_realized_total"] == pytest.approx(45.0)
    assert report["pnl_unrealized_avg"] == pytest.approx(15.0)
    assert report["exposure_avg"] == pytest.approx(750.0)
    assert report["pnl_unrealized_samples"] == 2
    assert report["slippage_avg_bps"] == pytest.approx(0.85)
    assert report["slippage_samples"] == 2
    assert report["hold_events"] == 2
    assert report["hold_breakdown"]["safety_hold"] == 1
    assert report["hold_breakdown"]["risk_throttle"] == 1


def test_append_and_load_daily_report(monkeypatch, tmp_path) -> None:
    path = tmp_path / "daily_reports.json"
    monkeypatch.setenv("DAILY_REPORTS_PATH", str(path))
    payload = {
        "timestamp": "2024-05-01T00:00:00+00:00",
        "pnl_realized_total": 12.5,
        "pnl_unrealized_avg": 4.2,
    }

    saved = daily_reporter.append_report(payload)
    assert path.exists()
    latest = daily_reporter.load_latest_report()
    assert latest is not None
    assert latest["pnl_realized_total"] == pytest.approx(12.5)
    assert latest["pnl_unrealized_avg"] == pytest.approx(4.2)
    assert "timestamp" in saved
