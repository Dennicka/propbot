from __future__ import annotations

from app.services import live_readiness


class _StubWatchdog:
    def __init__(self, ok: bool) -> None:
        self._ok = ok

    def overall_ok(self) -> bool:
        return self._ok


class _StubDailyLossCap:
    def __init__(self, breached: bool) -> None:
        self._breached = breached

    def is_breached(self) -> bool:
        return self._breached


def test_live_readiness_ready(client, monkeypatch):
    monkeypatch.setattr(live_readiness, "is_hold_active", lambda: False)
    monkeypatch.setattr(
        live_readiness,
        "get_exchange_watchdog",
        lambda: _StubWatchdog(ok=True),
    )
    monkeypatch.setattr(
        live_readiness,
        "get_daily_loss_cap",
        lambda: _StubDailyLossCap(breached=False),
    )
    monkeypatch.setattr(
        live_readiness.FeatureFlags,
        "enforce_daily_loss_cap",
        classmethod(lambda cls: True),
    )
    monkeypatch.setattr(
        live_readiness,
        "_universe_has_tradeable_instruments",
        lambda manager=None: True,
    )

    response = client.get("/live-readiness")

    assert response.status_code == 200
    assert response.json() == {
        "ready": True,
        "hold": False,
        "watchdog_ok": True,
        "daily_loss_breached": False,
        "universe_loaded": True,
        "reasons": [],
    }


def test_live_readiness_not_ready_due_to_hold(client, monkeypatch):
    monkeypatch.setattr(live_readiness, "is_hold_active", lambda: True)
    monkeypatch.setattr(
        live_readiness,
        "get_exchange_watchdog",
        lambda: _StubWatchdog(ok=True),
    )
    monkeypatch.setattr(
        live_readiness,
        "get_daily_loss_cap",
        lambda: _StubDailyLossCap(breached=False),
    )
    monkeypatch.setattr(
        live_readiness.FeatureFlags,
        "enforce_daily_loss_cap",
        classmethod(lambda cls: True),
    )
    monkeypatch.setattr(
        live_readiness,
        "_universe_has_tradeable_instruments",
        lambda manager=None: True,
    )

    response = client.get("/live-readiness")

    payload = response.json()
    assert response.status_code == 503
    assert payload["ready"] is False
    assert payload["hold"] is True
    assert "Global HOLD is active" in payload["reasons"]
