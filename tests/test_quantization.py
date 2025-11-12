from decimal import Decimal

import pytest

from app.orders.quantization import as_dec, quantize_order
from app.rules.pretrade import PretradeRejection, SymbolSpecs


def _meta(*, tick: float = 0.1, lot: float = 0.01, min_notional: float = 10.0) -> SymbolSpecs:
    return SymbolSpecs(
        symbol="BTCUSDT",
        tick=tick,
        lot=lot,
        min_notional=min_notional,
    )


def test_as_dec_converts_inputs_and_handles_none():
    assert as_dec(Decimal("1.5")) == Decimal("1.5")
    assert as_dec("2.25") == Decimal("2.25")
    assert as_dec(None, allow_none=True) is None
    with pytest.raises(PretradeRejection):
        as_dec(None)


def test_quantize_order_passes_through_valid_values():
    meta = _meta()
    price = as_dec("100.5", field="price")
    qty = as_dec("1.23", field="qty")

    q_price, q_qty = quantize_order("buy", price, qty, meta)

    assert q_price == Decimal("100.5")
    assert q_qty == Decimal("1.23")


def test_quantize_order_raises_on_step_violation():
    meta = _meta(lot=0.1)
    price = as_dec("50", field="price")
    qty = as_dec("1.234", field="qty")

    with pytest.raises(PretradeRejection) as excinfo:
        quantize_order("sell", price, qty, meta)

    assert excinfo.value.reason == "qty_step"
