from __future__ import annotations

from app.services import live_readiness
from app.services.runtime_badges import BADGE_AUTO_HOLD, BADGE_BREACH, BADGE_OK


def test_live_readiness_ok(client, monkeypatch):
    monkeypatch.setattr(
        live_readiness,
        "get_runtime_badges",
        lambda: {"watchdog": BADGE_OK, "daily_loss": BADGE_OK},
    )

    response = client.get("/live-readiness")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "reasons": []}


def test_live_readiness_not_ok_due_to_watchdog(client, monkeypatch):
    monkeypatch.setattr(
        live_readiness,
        "get_runtime_badges",
        lambda: {"watchdog": BADGE_AUTO_HOLD, "daily_loss": BADGE_OK},
    )

    response = client.get("/live-readiness")

    assert response.status_code == 503
    assert response.json() == {"ok": False, "reasons": ["watchdog:auto_hold"]}


def test_live_readiness_not_ok_due_to_daily_loss(client, monkeypatch):
    monkeypatch.setattr(
        live_readiness,
        "get_runtime_badges",
        lambda: {"watchdog": BADGE_OK, "daily_loss": BADGE_BREACH},
    )

    response = client.get("/live-readiness")

    assert response.status_code == 503
    assert response.json() == {"ok": False, "reasons": ["daily_loss:breach"]}
