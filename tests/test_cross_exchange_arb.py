from __future__ import annotations

from typing import Any, Dict

import pytest

from services import cross_exchange_arb
from services.cross_exchange_arb import _ExchangeClients
from services.risk_manager import can_open_new_position, reset_positions


class StubClient:
    def __init__(self, name: str, bid: float, ask: float) -> None:
        self.name = name
        self._bid = bid
        self._ask = ask
        self.open_orders: list[Dict[str, Any]] = []

    def get_best_bid_ask(self, symbol: str) -> Dict[str, Any]:
        return {"symbol": symbol, "bid": self._bid, "ask": self._ask}

    def open_long(self, symbol: str, qty_usdt: float, leverage: float) -> Dict[str, Any]:
        order = {
            "exchange": self.name,
            "symbol": symbol,
            "side": "long",
            "notional_usdt": qty_usdt,
            "leverage": leverage,
        }
        self.open_orders.append(order)
        return order

    def open_short(self, symbol: str, qty_usdt: float, leverage: float) -> Dict[str, Any]:
        order = {
            "exchange": self.name,
            "symbol": symbol,
            "side": "short",
            "notional_usdt": qty_usdt,
            "leverage": leverage,
        }
        self.open_orders.append(order)
        return order

    def close_position(self, symbol: str) -> Dict[str, Any]:  # pragma: no cover - unused in tests
        return {"exchange": self.name, "symbol": symbol, "status": "closed"}


@pytest.fixture(autouse=True)
def _reset_state():
    reset_positions()
    yield
    reset_positions()


@pytest.fixture
def stub_clients(monkeypatch):
    binance_stub = StubClient("binance", bid=20500.0, ask=20499.0)
    okx_stub = StubClient("okx", bid=20510.0, ask=20505.0)
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
    binance_stub = StubClient("binance", bid=20500.0, ask=20490.0)
    okx_stub = StubClient("okx", bid=20550.0, ask=20540.0)
    monkeypatch.setattr(
        cross_exchange_arb,
        "_clients",
        _ExchangeClients(binance=binance_stub, okx=okx_stub),
    )
    result = cross_exchange_arb.execute_hedged_trade(
        "ETHUSDT", notion_usdt=1000.0, leverage=3.0, min_spread=20.0
    )
    assert result["success"] is True
    assert result["long_order"]["exchange"] == "binance"
    assert result["short_order"]["exchange"] == "okx"


def test_risk_limits_block(monkeypatch):
    allowed, reason = can_open_new_position(60000.0, 2.0)
    assert not allowed
    assert reason == "notional_limit_exceeded"
