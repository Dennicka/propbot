import pytest

from app.services import runtime
from services import edge_guard


@pytest.fixture(autouse=True)
def _stub_balances(monkeypatch):
    monkeypatch.setattr(
        edge_guard.balances_monitor,
        "evaluate_balances",
        lambda: {"per_venue": {}, "liquidity_blocked": False, "reason": "ok"},
    )


@pytest.fixture(autouse=True)
def _reset_runtime():
    runtime.reset_for_tests()
    yield
    runtime.reset_for_tests()


def _stub_safety(monkeypatch, *, hold: bool = False, reason: str = "") -> None:
    monkeypatch.setattr(
        edge_guard.runtime,
        "get_safety_status",
        lambda: {"hold_active": hold, "hold_reason": reason},
    )


def test_edge_guard_blocks_on_partial_hedge(monkeypatch):
    _stub_safety(monkeypatch)
    monkeypatch.setattr(edge_guard, "_current_positions", lambda: [{"status": "partial"}])
    monkeypatch.setattr(edge_guard, "_avg_slippage", lambda symbol: (None, None))
    monkeypatch.setattr(edge_guard, "_pnl_downtrend_with_exposure", lambda: (False, 0.0))

    allowed, reason = edge_guard.allowed_to_trade("BTCUSDT")

    assert allowed is False
    assert reason == "partial_hedge_outstanding"


def test_edge_guard_blocks_on_slippage(monkeypatch):
    _stub_safety(monkeypatch)
    monkeypatch.setattr(edge_guard, "_current_positions", lambda: [])
    monkeypatch.setattr(edge_guard, "_avg_slippage", lambda symbol: (12.5, 0.0))
    monkeypatch.setattr(edge_guard, "_pnl_downtrend_with_exposure", lambda: (False, 0.0))

    allowed, reason = edge_guard.allowed_to_trade("ETHUSDT")

    assert allowed is False
    assert reason == "slippage_degraded"


def test_edge_guard_allows_in_normal_conditions(monkeypatch):
    _stub_safety(monkeypatch)
    monkeypatch.setattr(edge_guard, "_current_positions", lambda: [])
    monkeypatch.setattr(edge_guard, "_avg_slippage", lambda symbol: (2.0, 0.1))
    monkeypatch.setattr(edge_guard, "_pnl_downtrend_with_exposure", lambda: (False, 20_000.0))

    allowed, reason = edge_guard.allowed_to_trade("SOLUSDT")

    assert allowed is True
    assert reason == "ok"


def test_edge_guard_blocks_on_liquidity(monkeypatch):
    _stub_safety(monkeypatch)
    monkeypatch.setattr(
        edge_guard.balances_monitor,
        "evaluate_balances",
        lambda: {
            "per_venue": {
                "binance": {
                    "free_usdt": 25.0,
                    "used_usdt": 75.0,
                    "risk_ok": False,
                    "reason": "free balance below hedge size",
                }
            },
            "liquidity_blocked": True,
            "reason": "binance:free balance below hedge size",
        },
    )

    allowed, reason = edge_guard.allowed_to_trade("BTCUSDT")

    assert allowed is False
    assert reason == "binance:free balance below hedge size"
