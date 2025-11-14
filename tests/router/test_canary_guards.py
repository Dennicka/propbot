from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.router.order_router import OrderRouter, PretradeGateThrottled
from app.router import order_router as order_router_module


class DummyBroker:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_order(self, **payload):
        self.calls.append(payload)
        return {"broker_order_id": "BRK-1"}


def _install_stubs(monkeypatch: pytest.MonkeyPatch, *, is_canary: bool) -> None:
    monkeypatch.setattr(order_router_module.ledger, "fetch_positions", lambda: [])
    monkeypatch.setattr(
        order_router_module,
        "get_freeze_registry",
        lambda: SimpleNamespace(is_frozen=lambda **_: False),
    )

    class _Gate:
        def check_allowed(self, payload):
            return True, None

    monkeypatch.setattr(order_router_module.runtime, "get_pre_trade_gate", lambda: _Gate())
    monkeypatch.setattr(order_router_module.runtime, "record_pretrade_block", lambda *_, **__: None)
    safe_state = SimpleNamespace(state=SimpleNamespace(value="normal"), reason=None)
    monkeypatch.setattr(order_router_module, "get_safe_mode_state", lambda: safe_state)
    monkeypatch.setattr(order_router_module, "is_opening_allowed", lambda: True)
    allowed_result = SimpleNamespace(allowed=True, projected=Decimal("0"), limit=None)
    monkeypatch.setattr(
        order_router_module, "check_symbol_notional", lambda *_, **__: allowed_result
    )
    monkeypatch.setattr(
        order_router_module, "check_global_notional", lambda *_, **__: allowed_result
    )
    monkeypatch.setattr(order_router_module, "check_daily_loss", lambda *_, **__: allowed_result)
    monkeypatch.setattr(
        order_router_module, "get_daily_loss_cap_state", lambda: {"losses_usdt": Decimal("0")}
    )
    monkeypatch.setattr(order_router_module, "check_open_allowed", lambda *_, **__: (True, None))
    monkeypatch.setattr(order_router_module, "resolve_caps", lambda *_, **__: {})
    monkeypatch.setattr(
        order_router_module,
        "collect_snapshot",
        lambda: SimpleNamespace(
            by_venue_symbol={}, by_symbol={}, by_symbol_side={}, total_notional=Decimal("0")
        ),
    )
    monkeypatch.setattr(
        order_router_module,
        "snapshot_entry",
        lambda snapshot, symbol, venue: (
            {
                "base_qty": 0.0,
                "avg_price": 0.0,
                "LONG": 0.0,
                "SHORT": 0.0,
                "total_abs": 0.0,
                "side": "FLAT",
            },
            symbol,
            venue,
        ),
    )
    monkeypatch.setattr(order_router_module.ledger, "record_event", lambda *_, **__: None)
    monkeypatch.setattr(order_router_module, "enter_hold", lambda *_, **__: None)

    profile = SimpleNamespace(
        name="test-profile",
        allow_new_orders=True,
        allow_closures_only=False,
        max_notional_per_order=Decimal("0"),
    )
    monkeypatch.setattr(order_router_module, "get_trading_profile", lambda: profile)

    def _quantize(**payload):
        qty = Decimal(str(payload.get("qty")))
        price = payload.get("price")
        price_value = None if price is None else Decimal(str(price))
        return qty, price_value

    monkeypatch.setattr(order_router_module, "ensure_order_quantized", _quantize)

    class _Validator:
        def load_specs(self, payload):
            return SimpleNamespace(lot=None, tick=None, min_notional=None)

        def validate(self, payload):
            return True, None, None

    monkeypatch.setattr(order_router_module, "get_pretrade_validator", lambda: _Validator())

    runtime_profile = SimpleNamespace(
        name="paper",
        display_name="paper-canary" if is_canary else "paper",
        is_canary=is_canary,
        allow_trading=True,
        strict_flags=False,
    )
    monkeypatch.setattr(order_router_module.runtime, "get_profile", lambda: runtime_profile)


@pytest.mark.asyncio
async def test_canary_order_below_limit_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stubs(monkeypatch, is_canary=True)
    monkeypatch.setenv("CANARY_MAX_ORDER_NOTIONAL_USD", "200")
    monkeypatch.setenv("CANARY_MAX_DAILY_ORDERS", "5")

    broker = DummyBroker()
    router = OrderRouter(broker)

    result = await router.submit_order(
        account="acct",
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        order_type="LIMIT",
        qty=1.0,
        price=150.0,
        tif="GTC",
        strategy="test",
        request_id="req-ok",
    )

    assert result.broker_order_id == "BRK-1"
    assert len(broker.calls) == 1


@pytest.mark.asyncio
async def test_canary_order_above_limit_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stubs(monkeypatch, is_canary=True)
    monkeypatch.setenv("CANARY_MAX_ORDER_NOTIONAL_USD", "200")
    monkeypatch.setenv("CANARY_MAX_DAILY_ORDERS", "5")

    broker = DummyBroker()
    router = OrderRouter(broker)

    with pytest.raises(PretradeGateThrottled) as exc:
        await router.submit_order(
            account="acct",
            venue="binance",
            symbol="BTCUSDT",
            side="buy",
            order_type="LIMIT",
            qty=1.0,
            price=300.0,
            tif="GTC",
            strategy="test",
            request_id="req-block",
        )

    assert exc.value.reason == "canary-max-order-notional"
    assert broker.calls == []


@pytest.mark.asyncio
async def test_canary_daily_order_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stubs(monkeypatch, is_canary=True)
    monkeypatch.setenv("CANARY_MAX_ORDER_NOTIONAL_USD", "200")
    monkeypatch.setenv("CANARY_MAX_DAILY_ORDERS", "2")

    broker = DummyBroker()
    router = OrderRouter(broker)

    for idx in range(2):
        await router.submit_order(
            account="acct",
            venue="binance",
            symbol="BTCUSDT",
            side="buy",
            order_type="LIMIT",
            qty=1.0,
            price=50.0,
            tif="GTC",
            strategy="test",
            request_id=f"req-{idx}",
        )

    with pytest.raises(PretradeGateThrottled) as exc:
        await router.submit_order(
            account="acct",
            venue="binance",
            symbol="BTCUSDT",
            side="buy",
            order_type="LIMIT",
            qty=1.0,
            price=60.0,
            tif="GTC",
            strategy="test",
            request_id="req-limit",
        )

    assert exc.value.reason == "canary-max-daily-orders"
    assert len(broker.calls) == 2
