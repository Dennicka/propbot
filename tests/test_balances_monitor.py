import pytest

from app.services import runtime
from services import balances_monitor


@pytest.fixture(autouse=True)
def _reset_runtime():
    runtime.reset_for_tests()
    yield
    runtime.reset_for_tests()


class DummyClient:
    def __init__(self, payload):
        self._payload = payload

    def get_account_limits(self):
        return dict(self._payload)


def test_evaluate_balances_flags_low_free_balance(monkeypatch):
    runtime.apply_control_patch({"order_notional_usdt": 200.0})
    monkeypatch.setitem(
        balances_monitor._CLIENTS,
        "binance",
        DummyClient({"available_balance": 150.0, "total_balance": 500.0}),
    )
    monkeypatch.setitem(
        balances_monitor._CLIENTS,
        "okx",
        DummyClient({"available_balance": 1_500.0, "total_equity": 2_000.0}),
    )

    result = balances_monitor.evaluate_balances(auto_hold=False)

    assert result["liquidity_blocked"] is True
    binance_entry = result["per_venue"]["binance"]
    assert binance_entry["risk_ok"] is False
    assert "free balance below hedge size" in binance_entry["reason"]

    liquidity_status = runtime.get_liquidity_status()
    assert liquidity_status["liquidity_blocked"] is True
    assert "binance" in liquidity_status["reason"]


def test_evaluate_balances_in_dry_run_uses_mock(monkeypatch):
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    runtime.reset_for_tests()

    result = balances_monitor.evaluate_balances()

    assert result["liquidity_blocked"] is False
    assert all(entry["risk_ok"] for entry in result["per_venue"].values())
    liquidity_status = runtime.get_liquidity_status()
    assert liquidity_status["liquidity_blocked"] is False
    assert liquidity_status["reason"] == "dry_run_mode"


def test_evaluate_balances_detects_margin_pressure(monkeypatch):
    runtime.apply_control_patch({"order_notional_usdt": 50.0})
    monkeypatch.setitem(
        balances_monitor._CLIENTS,
        "binance",
        DummyClient({
            "available_balance": 500.0,
            "total_balance": 600.0,
            "margin_ratio": 0.85,
        }),
    )
    monkeypatch.setitem(
        balances_monitor._CLIENTS,
        "okx",
        DummyClient({
            "available_balance": 500.0,
            "total_equity": 600.0,
            "mgnRatio": 0.2,
        }),
    )

    result = balances_monitor.evaluate_balances(auto_hold=False)

    assert result["liquidity_blocked"] is True
    assert result["per_venue"]["binance"]["risk_ok"] is False
    assert "margin ratio" in result["per_venue"]["binance"]["reason"]
