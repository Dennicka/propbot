from __future__ import annotations

from typing import Any, Dict

import pytest

from app.services import runtime

from services import cross_exchange_arb, edge_guard
from services.cross_exchange_arb import _ExchangeClients
from services.risk_manager import can_open_new_position, reset_positions


class StubClient:
    def __init__(self, name: str, bid: float, ask: float) -> None:
        self.name = name
        self._bid = bid
        self._ask = ask
        self.placed_orders: list[Dict[str, Any]] = []

    def get_mark_price(self, symbol: str) -> Dict[str, Any]:
        # Use ask for mark to keep expectations deterministic.
        return {"symbol": symbol, "mark_price": float(self._ask)}

    def get_position(self, symbol: str) -> Dict[str, Any]:  # pragma: no cover - unused
        return {"symbol": symbol, "size": 0.0, "side": "flat"}

    def place_order(
        self, symbol: str, side: str, notional_usdt: float, leverage: float
    ) -> Dict[str, Any]:
        price = float(self._ask if side == "long" else self._bid)
        qty = float(notional_usdt) / price if price else 0.0
        order = {
            "exchange": self.name,
            "symbol": symbol,
            "side": side,
            "avg_price": price,
            "filled_qty": qty,
            "status": "filled",
            "order_id": f"{self.name}-order",
            "notional_usdt": float(notional_usdt),
            "leverage": float(leverage),
        }
        self.placed_orders.append(order)
        return order

    def cancel_all(self, symbol: str) -> Dict[str, Any]:  # pragma: no cover - unused
        return {"exchange": self.name, "symbol": symbol, "status": "cancelled"}

    def get_account_limits(self) -> Dict[str, Any]:  # pragma: no cover - unused
        return {"exchange": self.name, "available_balance": 0.0}


@pytest.fixture(autouse=True)
def _stub_liquidity(monkeypatch):
    monkeypatch.setattr(
        edge_guard.balances_monitor,
        "evaluate_balances",
        lambda: {"per_venue": {}, "liquidity_blocked": False, "reason": "ok"},
    )


@pytest.fixture(autouse=True)
def _reset_state():
    reset_positions()
    yield
    reset_positions()


@pytest.fixture
def stub_clients(monkeypatch):
    binance_stub = StubClient("binance", bid=20500.0, ask=20499.0)
    okx_stub = StubClient("okx", bid=20510.0, ask=20510.0)
    monkeypatch.setattr(
        cross_exchange_arb,
        "_clients",
        _ExchangeClients(binance=binance_stub, okx=okx_stub),
    )
    return binance_stub, okx_stub


def test_check_spread_identifies_exchanges(stub_clients):
    info = cross_exchange_arb.check_spread("BTCUSDT")
    assert info["cheap"] == "binance"
    assert info["expensive"] == "okx"
    assert info["spread"] == pytest.approx(11.0)


def test_execute_hedged_trade_success(monkeypatch):
    runtime.reset_for_tests()
    runtime.record_resume_request("cross_execute", requested_by="pytest")
    runtime.approve_resume(actor="pytest")
    state = runtime.get_state()
    state.control.safe_mode = False
    state.control.mode = "RUN"
    binance_stub = StubClient("binance", bid=20500.0, ask=20490.0)
    okx_stub = StubClient("okx", bid=20550.0, ask=20540.0)
    monkeypatch.setattr(
        cross_exchange_arb,
        "_clients",
        _ExchangeClients(binance=binance_stub, okx=okx_stub),
    )

    def fake_choose_venue(side: str, symbol: str, size: float) -> dict:
        if side.lower() in {"long", "buy"}:
            price = 20490.0
            venue = "binance"
        else:
            price = 20550.0
            venue = "okx"
        return {
            "venue": venue,
            "expected_fill_px": price,
            "fee_bps": 2,
            "liquidity_ok": True,
            "size": size,
            "expected_notional": size * price,
        }

    monkeypatch.setattr(cross_exchange_arb, "choose_venue", fake_choose_venue)
    monkeypatch.setattr(cross_exchange_arb, "_record_execution_stat", lambda **_: None)
    result = cross_exchange_arb.execute_hedged_trade(
        "ETHUSDT", notion_usdt=1000.0, leverage=3.0, min_spread=20.0
    )
    assert result["success"] is True
    assert result["status"] == "executed"
    assert result["long_order"]["exchange"] == "binance"
    assert result["short_order"]["exchange"] == "okx"
    assert all(leg["status"] == "filled" for leg in result["legs"])


def test_risk_limits_block(monkeypatch):
    allowed, reason = can_open_new_position(60000.0, 2.0)
    assert not allowed
    assert reason == "per_position_limit_exceeded"
