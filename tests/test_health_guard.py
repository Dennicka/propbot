from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config.schema import HealthConfig
from app.health.account_health import AccountHealthSnapshot
from app.pretrade.gate import PreTradeGate
from app.risk.guards import health_guard as health_guard_module
from app.risk.guards.health_guard import AccountHealthGuard


class FakeRuntime:
    def __init__(self, health_config: HealthConfig) -> None:
        self.pre_trade_gate = PreTradeGate()
        self.throttle_calls: list[tuple[bool, str | None, str | None]] = []
        self.hold_calls: list[tuple[str, str]] = []
        self.resume_calls: list[bool] = []
        self.auto_trade_states: list[bool] = []
        self.guard_updates: list[tuple[str, str, str, dict[str, object]]] = []
        self.state = SimpleNamespace(
            control=SimpleNamespace(safe_mode=False, auto_loop=False),
            safety=SimpleNamespace(
                hold_active=False,
                hold_reason=None,
                risk_throttled=False,
                risk_throttle_reason=None,
            ),
            config=SimpleNamespace(data=SimpleNamespace(health=health_config)),
        )

    def update_risk_throttle(self, active: bool, *, reason: str | None = None, source: str | None = None) -> None:
        self.throttle_calls.append((active, reason, source))
        self.state.safety.risk_throttled = bool(active)
        self.state.safety.risk_throttle_reason = reason if active else None

    def engage_safety_hold(self, reason: str, *, source: str = "") -> bool:
        self.hold_calls.append((reason, source))
        if self.state.safety.hold_active:
            self.state.safety.hold_reason = reason
            return False
        self.state.safety.hold_active = True
        self.state.safety.hold_reason = reason
        return True

    def autopilot_apply_resume(self, *, safe_mode: bool) -> dict[str, object]:
        self.resume_calls.append(safe_mode)
        self.state.safety.hold_active = False
        self.state.safety.hold_reason = None
        self.state.control.safe_mode = safe_mode
        self.state.control.auto_loop = True
        self.set_auto_trade_state(True)
        return {"hold_cleared": True}

    def set_auto_trade_state(self, value: bool) -> None:
        self.auto_trade_states.append(value)

    def get_state(self):  # pragma: no cover - trivial accessor
        return self.state

    def update_guard(self, name: str, status: str, summary: str, metrics: dict[str, object]) -> None:
        self.guard_updates.append((name, status, summary, metrics))

    def get_pre_trade_gate(self) -> PreTradeGate:
        return self.pre_trade_gate


def _make_snapshot(exchange: str, margin_ratio: float, *, collateral: float = 500.0) -> AccountHealthSnapshot:
    return AccountHealthSnapshot(
        exchange=exchange,
        equity_usdt=1000.0,
        free_collateral_usdt=collateral,
        init_margin_usdt=100.0,
        maint_margin_usdt=margin_ratio * 1000.0,
        margin_ratio=margin_ratio,
        ts=0.0,
    )


def _sequence_stub(items: list[dict[str, AccountHealthSnapshot]]):
    iterator = iter(items)
    last = items[-1]

    def _inner(_ctx: object) -> dict[str, AccountHealthSnapshot]:
        nonlocal last
        try:
            last = next(iterator)
        except StopIteration:
            pass
        return last

    return _inner


@pytest.fixture
def health_config() -> HealthConfig:
    return HealthConfig(guard_enabled=True)


def test_warn_throttles_risk_not_pretrade(monkeypatch: pytest.MonkeyPatch, health_config: HealthConfig) -> None:
    runtime = FakeRuntime(health_config)
    ctx = SimpleNamespace(runtime=runtime, config=SimpleNamespace(health=health_config))

    monkeypatch.setattr(
        health_guard_module,
        "collect_account_health",
        lambda _ctx: {"binance": _make_snapshot("binance", 0.8)},
    )
    metrics_calls: list[tuple[bool, str | None]] = []
    monkeypatch.setattr(
        health_guard_module,
        "set_risk_throttled",
        lambda active, reason: metrics_calls.append((active, reason)),
    )

    guard = AccountHealthGuard(lambda: ctx, SimpleNamespace(health=health_config))
    states, worst = guard.tick()

    assert worst == "WARN"
    assert states == {"binance": "WARN"}
    assert runtime.throttle_calls[-1] == (True, AccountHealthGuard.WARN_CAUSE, AccountHealthGuard.HOLD_SOURCE)
    assert runtime.pre_trade_gate.is_throttled is False
    assert runtime.hold_calls == []
    assert metrics_calls[-1] == (True, AccountHealthGuard.WARN_CAUSE)


def test_critical_sets_hold_and_pretrade_gate(monkeypatch: pytest.MonkeyPatch, health_config: HealthConfig) -> None:
    runtime = FakeRuntime(health_config)
    ctx = SimpleNamespace(runtime=runtime, config=SimpleNamespace(health=health_config))

    monkeypatch.setattr(
        health_guard_module,
        "collect_account_health",
        lambda _ctx: {"okx": _make_snapshot("okx", 0.9)},
    )
    metrics_calls: list[tuple[bool, str | None]] = []
    monkeypatch.setattr(
        health_guard_module,
        "set_risk_throttled",
        lambda active, reason: metrics_calls.append((active, reason)),
    )

    guard = AccountHealthGuard(lambda: ctx, SimpleNamespace(health=health_config))
    states, worst = guard.tick()

    assert worst == "CRITICAL"
    assert states == {"okx": "CRITICAL"}
    assert runtime.pre_trade_gate.is_throttled is True
    assert runtime.pre_trade_gate.reason == AccountHealthGuard.CRITICAL_CAUSE
    assert runtime.state.safety.hold_active is True
    assert runtime.state.safety.hold_reason == f"{AccountHealthGuard.CRITICAL_REASON_PREFIX}OKX"
    assert runtime.throttle_calls[-1] == (
        True,
        AccountHealthGuard.CRITICAL_CAUSE,
        AccountHealthGuard.HOLD_SOURCE,
    )
    assert metrics_calls[-1] == (True, AccountHealthGuard.CRITICAL_CAUSE)


def test_ok_hysteresis_clears(monkeypatch: pytest.MonkeyPatch, health_config: HealthConfig) -> None:
    runtime = FakeRuntime(health_config)
    ctx = SimpleNamespace(runtime=runtime, config=SimpleNamespace(health=health_config))

    snapshots = [
        {"bybit": _make_snapshot("bybit", 0.9)},
        {"bybit": _make_snapshot("bybit", 0.2)},
        {"bybit": _make_snapshot("bybit", 0.2)},
    ]
    monkeypatch.setattr(
        health_guard_module,
        "collect_account_health",
        _sequence_stub(snapshots),
    )
    metrics_calls: list[tuple[bool, str | None]] = []
    monkeypatch.setattr(
        health_guard_module,
        "set_risk_throttled",
        lambda active, reason: metrics_calls.append((active, reason)),
    )

    guard = AccountHealthGuard(lambda: ctx, SimpleNamespace(health=health_config))

    guard.tick()  # critical
    assert runtime.state.safety.hold_active is True
    assert runtime.pre_trade_gate.is_throttled is True

    guard.tick()  # first OK window
    assert runtime.state.safety.hold_active is True
    assert runtime.pre_trade_gate.is_throttled is True

    guard.tick()  # second OK window triggers clear
    assert runtime.state.safety.hold_active is False
    assert runtime.pre_trade_gate.is_throttled is False
    assert runtime.throttle_calls[-1] == (
        False,
        AccountHealthGuard.CRITICAL_CAUSE,
        AccountHealthGuard.HOLD_SOURCE,
    )
    assert metrics_calls[-1] == (False, AccountHealthGuard.CRITICAL_CAUSE)
    assert runtime.resume_calls == [False]
    assert runtime.auto_trade_states[-1] is True
