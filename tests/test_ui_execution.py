from decimal import Decimal

from app.pricing import TradeCostEstimate
from app.services import runtime


def test_ui_execution_includes_cost(client) -> None:
    runtime.reset_for_tests()
    estimate = TradeCostEstimate(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=Decimal("0.5"),
        price=Decimal("100"),
        taker_fee_bps=Decimal("2"),
        maker_fee_bps=Decimal("0"),
        estimated_fee=Decimal("0.1"),
        funding_rate=None,
        estimated_funding_cost=Decimal("0"),
        total_cost=Decimal("0.1"),
    )
    runtime.record_execution_order(
        {
            "client_order_id": "test-order",
            "venue": "binance-um",
            "symbol": "BTCUSDT",
            "side": "buy",
            "qty": 0.5,
            "price": 100.0,
            "strategy": "pytest",
            "ts_ns": 1,
            "created_ts": 0.0,
            "state": "NEW",
            "cost": estimate,
        }
    )

    response = client.get("/api/ui/execution")
    assert response.status_code == 200
    payload = response.json()
    assert "orders" in payload
    assert payload["orders"], "orders list should not be empty"
    order = payload["orders"][0]
    assert order["client_order_id"] == "test-order"
    assert order["cost"]["total_cost"] == str(estimate.total_cost)
    assert order["cost"]["estimated_fee"] == str(estimate.estimated_fee)
