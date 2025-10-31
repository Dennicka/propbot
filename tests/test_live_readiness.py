from __future__ import annotations

from app.runtime import leader_lock
from app.services import live_readiness
from app.services.runtime_badges import BADGE_AUTO_HOLD, BADGE_BREACH, BADGE_OK


def _mock_health(monkeypatch, *, ok=True, journal_ok=True, config_ok=True):
    monkeypatch.setattr(
        live_readiness,
        "evaluate_health",
        lambda _app: {
            "ok": ok,
            "journal_ok": journal_ok,
            "config_ok": config_ok,
        },
    )


def _mock_badges(monkeypatch, watchdog=BADGE_OK, daily=BADGE_OK):
    monkeypatch.setattr(
        live_readiness,
        "get_runtime_badges",
        lambda: {"watchdog": watchdog, "daily_loss": daily},
    )


def test_live_readiness_ok(client, monkeypatch):
    _mock_badges(monkeypatch)
    _mock_health(monkeypatch)
    monkeypatch.setattr(live_readiness, "leader_feature_enabled", lambda: False)

    response = client.get("/live-readiness")

    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "reasons": [],
        "leader": True,
        "health_ok": True,
        "journal_ok": True,
        "config_ok": True,
    }


def test_live_readiness_not_ok_due_to_watchdog(client, monkeypatch):
    _mock_badges(monkeypatch, watchdog=BADGE_AUTO_HOLD)
    _mock_health(monkeypatch)
    monkeypatch.setattr(live_readiness, "leader_feature_enabled", lambda: False)

    response = client.get("/live-readiness")

    payload = response.json()
    assert response.status_code == 503
    assert "watchdog:auto_hold" in payload["reasons"]
    assert payload["ready"] is False


def test_live_readiness_not_ok_due_to_daily_loss(client, monkeypatch):
    _mock_badges(monkeypatch, daily=BADGE_BREACH)
    _mock_health(monkeypatch)
    monkeypatch.setattr(live_readiness, "leader_feature_enabled", lambda: False)

    response = client.get("/live-readiness")

    payload = response.json()
    assert response.status_code == 503
    assert "daily_loss:breach" in payload["reasons"]
    assert payload["ready"] is False


def test_live_readiness_health_gate(client, monkeypatch):
    _mock_badges(monkeypatch)
    _mock_health(monkeypatch, ok=False)
    monkeypatch.setattr(live_readiness, "leader_feature_enabled", lambda: False)

    response = client.get("/live-readiness")
    payload = response.json()

    assert response.status_code == 503
    assert "healthz:not_ok" in payload["reasons"]


def test_live_readiness_journal_gate(client, monkeypatch):
    _mock_badges(monkeypatch)
    _mock_health(monkeypatch, ok=False, journal_ok=False)
    monkeypatch.setattr(live_readiness, "leader_feature_enabled", lambda: False)

    response = client.get("/live-readiness")
    payload = response.json()

    assert response.status_code == 503
    assert "journal:not_ok" in payload["reasons"]


def test_live_readiness_config_gate(client, monkeypatch):
    _mock_badges(monkeypatch)
    _mock_health(monkeypatch, ok=True, journal_ok=True, config_ok=False)
    monkeypatch.setattr(live_readiness, "leader_feature_enabled", lambda: False)

    response = client.get("/live-readiness")
    payload = response.json()

    assert response.status_code == 503
    assert "config:not_ok" in payload["reasons"]


def test_live_readiness_second_instance_not_ready(client, monkeypatch):
    monkeypatch.setenv("FEATURE_LEADER_LOCK", "1")
    monkeypatch.setenv("LEADER_LOCK_TTL_SEC", "300")
    _mock_badges(monkeypatch)
    _mock_health(monkeypatch)

    leader_lock.reset_for_tests()
    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "primary")
    assert leader_lock.acquire() is True
    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "secondary")

    response = client.get("/live-readiness")
    payload = response.json()

    assert response.status_code == 503
    assert payload["ready"] is False
    assert any(reason.startswith("leader:") for reason in payload["reasons"])
