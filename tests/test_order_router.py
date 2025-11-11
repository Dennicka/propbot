from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.router.order_router import OrderRouter, PretradeValidationError
from app.router import order_router as order_router_module
from app.util.quantization import QuantizationError


class DummyBroker:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_order(self, **payload):  # pragma: no cover - patched out in tests
        self.calls.append(payload)
        return {"broker_order_id": "BRK-1"}


class StopProcessing(RuntimeError):
    """Sentinel used to abort order submission after quantisation."""


def _install_common_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(
        order_router_module,
        "check_symbol_notional",
        lambda *_, **__: SimpleNamespace(allowed=True, projected=Decimal("0"), limit=None),
    )
    monkeypatch.setattr(
        order_router_module,
        "check_global_notional",
        lambda *_, **__: SimpleNamespace(allowed=True, projected=Decimal("0"), limit=None),
    )
    monkeypatch.setattr(
        order_router_module,
        "check_daily_loss",
        lambda *_, **__: SimpleNamespace(allowed=True, projected=Decimal("0"), limit=None),
    )
    monkeypatch.setattr(
        order_router_module, "get_daily_loss_cap_state", lambda: {"losses_usdt": Decimal("0")}
    )
    monkeypatch.setattr(order_router_module, "check_open_allowed", lambda *_, **__: (True, None))
    monkeypatch.setattr(
        order_router_module,
        "resolve_caps",
        lambda *_, **__: {"global_max_abs": None, "side_max_abs": None, "venue_max_abs": None},
    )
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


@pytest.mark.asyncio
async def test_quantisation_failure_blocks_order(monkeypatch: pytest.MonkeyPatch):
    _install_common_stubs(monkeypatch)

    broker = DummyBroker()
    router = OrderRouter(broker)

    captured: dict[str, object] = {}

    def fake_quantize(**kwargs):
        captured.update(kwargs)
        raise QuantizationError(
            "qty_step", field="qty", value=Decimal("0.105"), limit=Decimal("0.01")
        )

    monkeypatch.setattr(order_router_module, "ensure_order_quantized", fake_quantize)

    class _Validator:
        def load_specs(self, payload):
            return SimpleNamespace(
                lot=Decimal("0.01"), tick=Decimal("0.1"), min_notional=Decimal("5")
            )

        def validate(self, payload):
            return True, None, None

    monkeypatch.setattr(order_router_module, "get_pretrade_validator", lambda: _Validator())

    with pytest.raises(PretradeValidationError) as exc:
        await router.submit_order(
            account="acct",
            venue="test",
            symbol="BTCUSDT",
            side="buy",
            order_type="LIMIT",
            qty=0.105,
            price=30000.0,
            tif="GTC",
            strategy="quant",
            request_id="req-1",
        )

    assert exc.value.reason == "qty_step"
    assert broker.calls == []
    assert captured["qty"] == pytest.approx(0.105)
    assert captured["step_size"] == Decimal("0.01")
    assert captured["tick_size"] == Decimal("0.1")
    assert captured["min_notional"] == Decimal("5")


@pytest.mark.asyncio
async def test_quantisation_uses_specs(monkeypatch: pytest.MonkeyPatch):
    _install_common_stubs(monkeypatch)

    broker = DummyBroker()
    router = OrderRouter(broker)

    quant_calls: dict[str, object] = {}

    def fake_quantize(**kwargs):
        quant_calls.update(kwargs)
        return Decimal("0.1"), Decimal("29999")

    monkeypatch.setattr(order_router_module, "ensure_order_quantized", fake_quantize)

    class _Validator:
        def load_specs(self, payload):
            return SimpleNamespace(
                lot=Decimal("0.01"), tick=Decimal("0.5"), min_notional=Decimal("10")
            )

        def validate(self, payload):
            return True, None, None

    monkeypatch.setattr(order_router_module, "get_pretrade_validator", lambda: _Validator())

    def fail_after_quantization():
        raise StopProcessing()

    monkeypatch.setattr(order_router_module, "collect_snapshot", fail_after_quantization)

    with pytest.raises(StopProcessing):
        await router.submit_order(
            account="acct",
            venue="test",
            symbol="ETHUSDT",
            side="sell",
            order_type="LIMIT",
            qty=1.0,
            price=29999.9,
            tif="GTC",
            strategy="quant",
            request_id="req-2",
        )

    assert quant_calls["step_size"] == Decimal("0.01")
    assert quant_calls["tick_size"] == Decimal("0.5")
    assert quant_calls["min_notional"] == Decimal("10")
    assert quant_calls["qty"] == pytest.approx(1.0)
    assert quant_calls["price"] == pytest.approx(29999.9)
    assert broker.calls == []
