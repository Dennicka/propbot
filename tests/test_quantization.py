from decimal import Decimal

from app.orders.quantization import as_dec, quantize_price, quantize_qty


def test_quantize_price_rounds_down() -> None:
    price = as_dec("20123.456")
    tick = as_dec("0.5")
    assert quantize_price(price, tick) == Decimal("20123.0")


def test_quantize_qty_rounds_down() -> None:
    qty = as_dec("0.123456")
    step = as_dec("0.001")
    assert quantize_qty(qty, step) == Decimal("0.123")


def test_quantize_handles_string_and_decimal_inputs() -> None:
    qty = as_dec("0.1234")
    step = Decimal("0.01")
    assert quantize_qty(qty, step) == Decimal("0.12")
    price = as_dec(Decimal("101.234"))
    tick = Decimal("0.1")
    assert quantize_price(price, tick) == Decimal("101.2")
