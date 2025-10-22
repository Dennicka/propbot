from __future__ import annotations

from datetime import datetime, timezone

from app import ledger


def test_record_order_idempotent_update():
    ledger.reset()
    ts = datetime.now(timezone.utc).isoformat()
    order_id = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.1,
        price=25_000.0,
        status="submitted",
        client_ts=ts,
        exchange_ts=None,
        idemp_key="order-key",
    )
    assert order_id > 0
    updated_id = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.1,
        price=24_500.0,
        status="open",
        client_ts=ts,
        exchange_ts=None,
        idemp_key="order-key",
    )
    assert updated_id == order_id
    stored = ledger.get_order(order_id)
    assert stored is not None
    assert stored["price"] == 24_500.0
    assert stored["status"] == "open"
