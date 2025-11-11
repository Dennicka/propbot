import pytest

from services import cross_exchange_arb as module


class StubClient:
    def __init__(self, venue: str, mark_price: float, *, fail_on_order: bool = False) -> None:
        self.venue = venue
        self.mark_price = mark_price
        self.fail_on_order = fail_on_order
        self.placed_orders = []

    def get_mark_price(self, symbol: str) -> dict:
        return {"symbol": symbol, "mark_price": float(self.mark_price)}

    def get_position(self, symbol: str) -> dict:  # pragma: no cover - unused but part of protocol
        return {"symbol": symbol, "size": 0.0, "side": "flat"}

    def place_order(self, symbol: str, side: str, notional_usdt: float, leverage: float) -> dict:
        if self.fail_on_order:
            raise RuntimeError(f"{self.venue}_order_failed")
        avg_price = float(self.mark_price)
        qty = float(notional_usdt) / avg_price if avg_price else 0.0
        order = {
            "exchange": self.venue,
            "symbol": symbol,
            "side": side,
            "avg_price": avg_price,
            "filled_qty": qty,
            "status": "filled",
            "order_id": f"{self.venue}-order",
            "notional_usdt": float(notional_usdt),
            "leverage": float(leverage),
        }
        self.placed_orders.append(order)
        return order

    def cancel_all(self, symbol: str) -> dict:  # pragma: no cover - unused in tests
        return {"exchange": self.venue, "symbol": symbol, "status": "cancelled"}

    def get_account_limits(self) -> dict:  # pragma: no cover - unused in tests
        return {"exchange": self.venue, "available_balance": 1_000_000.0}


@pytest.fixture(autouse=True)
def _reset_runtime(monkeypatch):
    monkeypatch.setattr(module, "register_order_attempt", lambda **_: None)
    monkeypatch.setattr(module, "append_entry", lambda entry: entry)
    monkeypatch.setattr(module, "_record_execution_stat", lambda **kwargs: None)
    monkeypatch.setattr(module, "guard_allowed_to_trade", lambda symbol: (True, "ok"))

    def fake_choose_venue(side: str, symbol: str, size: float) -> dict:
        if side.lower() in {"long", "buy"}:
            price = 100.0
            venue = "binance"
        else:
            price = 105.0
            venue = "okx"
        return {
            "venue": venue,
            "expected_fill_px": price,
            "fee_bps": 2,
            "liquidity_ok": True,
            "size": size,
            "expected_notional": size * price,
        }

    monkeypatch.setattr(module, "choose_venue", fake_choose_venue)


def test_execute_hedge_success(monkeypatch):
    binance = StubClient("binance", 100.0)
    okx = StubClient("okx", 105.0)
    monkeypatch.setattr(module, "_clients", module._ExchangeClients(binance=binance, okx=okx))
    monkeypatch.setattr(module, "is_dry_run_mode", lambda: False)
    monkeypatch.setattr(module, "engage_safety_hold", lambda *_, **__: None)

    result = module.execute_hedged_trade("BTCUSDT", 1_000.0, 2.0, 1.0)

    assert result["success"] is True
    assert result["status"] == "executed"
    assert len(result["legs"]) == 2
    assert binance.placed_orders and okx.placed_orders
    long_leg = result["long_order"]
    short_leg = result["short_order"]
    assert long_leg["status"] == "filled"
    assert short_leg["status"] == "filled"
    assert pytest.approx(long_leg["avg_price"]) == 100.0
    assert pytest.approx(short_leg["avg_price"]) == 105.0


def test_execute_hedge_partial_failure(monkeypatch):
    binance = StubClient("binance", 100.0)
    okx = StubClient("okx", 105.0, fail_on_order=True)
    monkeypatch.setattr(module, "_clients", module._ExchangeClients(binance=binance, okx=okx))
    monkeypatch.setattr(module, "is_dry_run_mode", lambda: False)
    logs: list[dict] = []
    monkeypatch.setattr(module, "append_entry", lambda entry: logs.append(entry))
    hold_triggered = {}
    monkeypatch.setattr(
        module,
        "engage_safety_hold",
        lambda reason, source=None: hold_triggered.update({"reason": reason, "source": source}),
    )

    result = module.execute_hedged_trade("BTCUSDT", 1_000.0, 2.0, 1.0)

    assert result["success"] is False
    assert result["reason"] == "short_leg_failed"
    assert result["hold_engaged"] is True
    assert hold_triggered["reason"] == "hedge_leg_failed"
    assert logs and logs[0]["status"] == "partial_failure"
    assert binance.placed_orders  # long leg executed


def test_execute_hedge_dry_run(monkeypatch):
    binance = StubClient("binance", 100.0, fail_on_order=True)
    okx = StubClient("okx", 105.0, fail_on_order=True)
    monkeypatch.setattr(module, "_clients", module._ExchangeClients(binance=binance, okx=okx))
    monkeypatch.setattr(module, "is_dry_run_mode", lambda: True)
    monkeypatch.setattr(module, "engage_safety_hold", lambda *_, **__: None)

    result = module.execute_hedged_trade("BTCUSDT", 1_000.0, 2.0, 1.0)

    assert result["success"] is True
    assert result["status"] == "simulated"
    assert all(leg["status"] == "simulated" for leg in result["legs"])
    assert not binance.placed_orders and not okx.placed_orders
