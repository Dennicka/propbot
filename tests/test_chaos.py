from __future__ import annotations

import app.utils.chaos as chaos


def test_chaos_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FEATURE_CHAOS", raising=False)
    monkeypatch.delenv("CHAOS_WS_DROP_P", raising=False)
    monkeypatch.delenv("CHAOS_REST_TIMEOUT_P", raising=False)
    monkeypatch.delenv("CHAOS_ORDER_DELAY_MS", raising=False)
    chaos.configure(None)

    settings = chaos.resolve_settings(None)
    assert settings.enabled is False
    assert settings.ws_drop_p == 0.0
    assert settings.rest_timeout_p == 0.0
    assert settings.order_delay_ms == 0

    chaos.configure(settings)
    monkeypatch.setattr(chaos.random, "random", lambda: 0.99)
    assert chaos.should_drop_ws_update() is False
    chaos.configure(None)


def test_chaos_env_overrides(monkeypatch):
    monkeypatch.setenv("FEATURE_CHAOS", "1")
    monkeypatch.setenv("CHAOS_WS_DROP_P", "0.5")
    monkeypatch.setenv("CHAOS_REST_TIMEOUT_P", "0.25")
    monkeypatch.setenv("CHAOS_ORDER_DELAY_MS", "123")
    chaos.configure(None)

    settings = chaos.resolve_settings(None)
    assert settings.enabled is True
    assert settings.ws_drop_p == 0.5
    assert settings.rest_timeout_p == 0.25
    assert settings.order_delay_ms == 123

    chaos.configure(settings)
    monkeypatch.setattr(chaos.random, "random", lambda: 0.4)
    assert chaos.should_drop_ws_update() is True
    monkeypatch.setattr(chaos.random, "random", lambda: 0.6)
    assert chaos.should_drop_ws_update() is False
    chaos.configure(None)

