from decimal import Decimal

import pytest

from app.pricing import estimate_trade_cost


def test_estimate_trade_cost_basic_fee_only():
    cost = estimate_trade_cost(
        venue="binance",
        symbol="BTCUSDT",
        side="buy",
        qty=Decimal("1"),
        price=Decimal("10000"),
        taker_fee_bps=Decimal("10"),
        funding_rate=None,
    )

    assert cost.estimated_fee == Decimal("10")
    assert cost.estimated_funding_cost == Decimal("0")
    assert cost.total_cost == Decimal("10")


def test_estimate_trade_cost_with_funding():
    cost = estimate_trade_cost(
        venue="binance",
        symbol="BTCUSDT",
        side="sell",
        qty=Decimal("2"),
        price=Decimal("5000"),
        taker_fee_bps=Decimal("5"),
        funding_rate=Decimal("0.0001"),
    )

    expected_fee = Decimal("2") * Decimal("5000") * Decimal("5") / Decimal("10000")
    expected_funding = Decimal("2") * Decimal("5000") * Decimal("0.0001")

    assert cost.estimated_fee == expected_fee
    assert cost.estimated_funding_cost == expected_funding
    assert cost.total_cost == expected_fee + expected_funding


@pytest.mark.parametrize(
    "side, qty, price, error_message",
    [
        ("buy", Decimal("0"), Decimal("100"), "qty must be positive"),
        ("sell", Decimal("1"), Decimal("0"), "price must be positive"),
        ("hold", Decimal("1"), Decimal("1"), "side must be either 'buy' or 'sell'"),
    ],
)
def test_estimate_trade_cost_invalid_args(side, qty, price, error_message):
    with pytest.raises(ValueError) as exc:
        estimate_trade_cost(
            venue="binance",
            symbol="BTCUSDT",
            side=side,
            qty=qty,
            price=price,
            taker_fee_bps=Decimal("1"),
        )

    assert error_message in str(exc.value)
