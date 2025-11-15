from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.approvals.live_toggle import LiveToggleEffectiveState
from app.runtime.live_guard import LiveGuardConfigView, LiveTradingGuard


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOW_LIVE_TRADING", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ALLOWED_VENUES", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ALLOWED_STRATEGIES", raising=False)


class _Store:
    def __init__(self, state: LiveToggleEffectiveState) -> None:
        self._state = state

    def get_effective_state(self) -> LiveToggleEffectiveState:
        return self._state


def _make_state(
    *, enabled: bool, action: str = "enable_live", status: str = "approved"
) -> LiveToggleEffectiveState:
    return LiveToggleEffectiveState(
        enabled=enabled,
        last_action=action,
        last_status=status,
        last_updated_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        last_request_id="req-123",
        requestor_id="alice",
        approver_id="bob" if status == "approved" else None,
        resolution_reason="ok" if status == "approved" else "denied",
    )


def _config(live_guard: LiveTradingGuard) -> LiveGuardConfigView:
    return live_guard.get_config_view()


def test_live_guard_disables_live_if_env_false_even_with_approvals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.runtime.live_guard.get_live_toggle_store", lambda: _Store(_make_state(enabled=True))
    )
    guard = LiveTradingGuard(runtime_profile="live")

    cfg = _config(guard)

    assert cfg.allow_live_trading is False
    assert cfg.state == "disabled"
    assert cfg.reason and "ALLOW_LIVE_TRADING" in cfg.reason
    assert cfg.approvals_enabled is True


def test_live_guard_disables_live_if_no_approvals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    monkeypatch.setattr(
        "app.runtime.live_guard.get_live_toggle_store",
        lambda: _Store(_make_state(enabled=False, action="disable_live", status="approved")),
    )
    guard = LiveTradingGuard(runtime_profile="live")

    cfg = _config(guard)

    assert cfg.allow_live_trading is False
    assert cfg.reason == "two-man approvals not granted"
    assert cfg.approvals_enabled is False
    assert cfg.approvals_last_action == "disable_live"
    assert cfg.approvals_last_status == "approved"


def test_live_guard_enables_live_only_when_env_and_approvals_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    monkeypatch.setenv("LIVE_TRADING_ALLOWED_VENUES", "binance_perp")
    monkeypatch.setenv("LIVE_TRADING_ALLOWED_STRATEGIES", "alpha")
    monkeypatch.setattr(
        "app.runtime.live_guard.get_live_toggle_store", lambda: _Store(_make_state(enabled=True))
    )

    guard = LiveTradingGuard(runtime_profile="live")
    cfg = _config(guard)

    assert cfg.allow_live_trading is True
    assert cfg.state == "enabled"
    assert cfg.reason is None
    assert cfg.allowed_venues == ["binance_perp"]
    assert cfg.allowed_strategies == ["alpha"]


def test_live_guard_exposes_approvals_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_LIVE_TRADING", "true")
    state = _make_state(enabled=True)
    monkeypatch.setattr("app.runtime.live_guard.get_live_toggle_store", lambda: _Store(state))
    guard = LiveTradingGuard(runtime_profile="live")

    cfg = _config(guard)

    assert cfg.approvals_enabled is True
    assert cfg.approvals_last_request_id == "req-123"
    assert cfg.approvals_requestor_id == "alice"
    assert cfg.approvals_approver_id == "bob"
    assert cfg.approvals_resolution_reason == "ok"
    assert cfg.approvals_last_updated_at == datetime(2024, 1, 1, tzinfo=timezone.utc)
