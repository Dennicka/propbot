from __future__ import annotations

from app.runtime import leader_lock
from app.services import live_readiness, runtime
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
        "fencing_id": None,
        "hb_age_sec": None,
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
    assert payload.get("fencing_id")


def test_live_readiness_hold_after_stale_takeover(client, monkeypatch):
    _mock_badges(monkeypatch)
    _mock_health(monkeypatch)
    monkeypatch.setenv("FEATURE_LEADER_LOCK", "1")
    monkeypatch.setenv("LEADER_LOCK_TTL_SEC", "10")
    monkeypatch.setenv("LEADER_LOCK_STALE_SEC", "5")

    leader_lock.reset_for_tests()
    base_time = 1_000.0

    # Primary acquires leadership and writes heartbeat.
    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "primary")
    monkeypatch.setattr(leader_lock, "_INSTANCE_ID", None)
    monkeypatch.setattr(leader_lock.time, "time", lambda: base_time)
    assert leader_lock.acquire(now=base_time) is True
    primary_status = leader_lock.get_status(now=base_time)
    primary_fencing = primary_status.get("fencing_id")
    assert primary_fencing

    # Secondary steals the lock after the heartbeat becomes stale.
    takeover_time = base_time + 12
    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "secondary")
    monkeypatch.setattr(leader_lock, "_INSTANCE_ID", None)
    monkeypatch.setattr(leader_lock.time, "time", lambda: takeover_time)
    assert leader_lock.acquire(now=takeover_time) is True
    secondary_status = leader_lock.get_status(now=takeover_time)
    secondary_fencing = secondary_status.get("fencing_id")
    assert secondary_fencing and secondary_fencing != primary_fencing

    # Prepare runtime state to observe HOLD transition.
    state = runtime.get_state()
    state.control.mode = "RUN"
    state.control.safe_mode = False

    # Primary performs readiness check after losing leadership.
    response_time = takeover_time + 1
    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "primary")
    monkeypatch.setattr(leader_lock, "_INSTANCE_ID", None)
    monkeypatch.setattr(leader_lock.time, "time", lambda: response_time)

    response = client.get("/live-readiness")
    payload = response.json()

    assert response.status_code == 503
    assert payload["ready"] is False
    assert payload["leader"] is False
    assert payload.get("fencing_id") == secondary_fencing
    assert isinstance(payload.get("hb_age_sec"), float)
    assert payload["hb_age_sec"] >= 1.0

    state_after = runtime.get_state()
    assert state_after.control.mode == "HOLD"
    assert state_after.control.safe_mode is True
