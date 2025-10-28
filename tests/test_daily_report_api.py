from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.daily_reporter import append_report


def test_daily_report_requires_token(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "daily-token")

    response = client.get("/api/ui/daily_report")
    assert response.status_code in {401, 403}


def test_daily_report_returns_payload(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "daily-token")

    append_report(
        {
            "timestamp": datetime(2024, 5, 1, 0, 0, tzinfo=timezone.utc).isoformat(),
            "pnl_realized_total": 123.45,
            "pnl_unrealized_avg": 67.89,
            "exposure_avg": 5000.0,
            "hold_events": 1,
            "hold_breakdown": {"safety_hold": 1, "risk_throttle": 0},
        }
    )

    response = client.get(
        "/api/ui/daily_report",
        headers={"Authorization": "Bearer daily-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload.get("available") is True
    assert payload["pnl_realized_total"] == pytest.approx(123.45)
    assert payload["pnl_unrealized_avg"] == pytest.approx(67.89)
    assert payload["hold_events"] == 1
    assert payload["hold_breakdown"]["safety_hold"] == 1
