from decimal import Decimal

import pytest

from app.util.quantization import QuantizationError, ensure_order_quantized


def test_quantized_values_pass_through():
    qty, price = ensure_order_quantized(
        qty=1.5,
        price=101.0,
        step_size=0.1,
        tick_size=0.5,
        min_notional=50,
    )

    assert qty == Decimal("1.5")
    assert price == Decimal("101.0")


def test_quantity_step_violation_raises():
    with pytest.raises(QuantizationError) as excinfo:
        ensure_order_quantized(qty=1.53, price=100.0, step_size=0.1, tick_size=None)

    error = excinfo.value
    assert error.reason == "qty_step"
    assert error.field == "qty"


def test_price_tick_violation_raises():
    with pytest.raises(QuantizationError) as excinfo:
        ensure_order_quantized(qty=1.0, price=101.07, step_size=None, tick_size=0.05)

    error = excinfo.value
    assert error.reason == "price_tick"
    assert error.field == "price"


def test_min_notional_violation_raises():
    with pytest.raises(QuantizationError) as excinfo:
        ensure_order_quantized(qty=0.5, price=10.0, step_size=0.1, tick_size=0.1, min_notional=10.1)

    error = excinfo.value
    assert error.reason == "min_notional"
    assert error.field == "notional"
