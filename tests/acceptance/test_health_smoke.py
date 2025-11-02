from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry

from app.health.account_health import AccountHealthSnapshot, register_metrics
from app.risk.guards.health_guard import AccountHealthGuard, build_health_guard_context
from app.router.order_router import enforce_reduce_only
from app.services import runtime


def _critical_snapshot(exchange: str) -> AccountHealthSnapshot:
    now = time.time()
    return AccountHealthSnapshot(
        exchange=exchange,
        equity_usdt=1000.0,
        free_collateral_usdt=5.0,
        init_margin_usdt=400.0,
        maint_margin_usdt=900.0,
        margin_ratio=0.9,
        ts=now,
    )


@pytest.mark.acceptance
def test_health_critical_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime.reset_for_tests()
    register_metrics(CollectorRegistry())

    monkeypatch.setattr(
        "app.risk.guards.health_guard.collect_account_health",
        lambda ctx: {"binance": _critical_snapshot("binance")},
    )

    ctx_factory, cfg = build_health_guard_context()
    guard = AccountHealthGuard(ctx_factory, cfg, env={"HEALTH_GUARD_ENABLED": "true"})

    states, worst = guard.tick()

    assert worst == "CRITICAL"
    assert states == {"binance": "CRITICAL"}

    state = runtime.get_state()
    assert state.control.mode == "HOLD"
    assert state.safety.hold_active is True
    assert state.safety.hold_reason == "ACCOUNT_HEALTH::CRITICAL::BINANCE"

    gate = runtime.get_pre_trade_gate()
    assert gate.is_throttled_by(AccountHealthGuard.CRITICAL_CAUSE)

    blocked_ctx = SimpleNamespace(pre_trade_gate=gate)
    allowed, native, reason = enforce_reduce_only(
        blocked_ctx,
        "BTCUSDT",
        "buy",
        1.0,
        {"qty": 2.0},
    )
    assert allowed is False
    assert native is False
    assert reason == "blocked: reduce-only due to ACCOUNT_HEALTH::CRITICAL"

    reducing_ctx = SimpleNamespace(pre_trade_gate=gate, native_reduce_only=True)
    reduce_allowed, native_flag, reduce_reason = enforce_reduce_only(
        reducing_ctx,
        "BTCUSDT",
        "sell",
        1.0,
        {"qty": 2.0},
    )
    assert reduce_allowed is True
    assert native_flag is True
    assert reduce_reason is None

    runtime.reset_for_tests()
