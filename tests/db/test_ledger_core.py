from __future__ import annotations

import time
from decimal import Decimal

import pytest

from app.db import ledger


def test_ledger_core_crud_and_aggregations(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db_path = tmp_path / "ledger.db"
    monkeypatch.setenv("LEDGER_DB_PATH", str(db_path))
    conn = ledger.init_db(str(db_path))

    order_id = "order-001"
    order_payload = {
        "order_id": order_id,
        "intent_key": "intent-001",
        "strategy": "alpha",
        "symbol": "BTCUSDT",
        "venue": "binance",
        "side": "BUY",
        "qty": Decimal("1"),
        "px": Decimal("100"),
    }
    assert ledger.upsert_order_begin(order_payload)
    assert ledger.mark_order_acked(order_id, "exch-001")

    first_fill_ts = time.time()
    assert ledger.append_fill(
        order_id,
        first_fill_ts,
        Decimal("0.5"),
        Decimal("100"),
        realized_pnl_usd=Decimal("5"),
    )
    assert ledger.append_fill(
        order_id,
        first_fill_ts + 1.0,
        Decimal("0.5"),
        Decimal("101"),
        realized_pnl_usd=Decimal("4"),
    )
    assert ledger.mark_order_final(order_id, "FILLED")

    statuses = ledger.fetch_orders_status()
    assert statuses[order_id] == "FILLED"

    positions = ledger.snapshot_positions()
    key = (order_payload["venue"], order_payload["symbol"])
    assert key in positions
    snapshot = positions[key]
    assert snapshot["net_qty"] == Decimal("1.0")
    assert snapshot["vwap"] > Decimal("0")

    day_key = time.strftime("%Y-%m-%d", time.gmtime(first_fill_ts))
    realized = ledger.realized_pnl_day(day_key)
    assert realized == Decimal("9")

    cur = conn.execute("SELECT COUNT(*) FROM fills WHERE order_id=?", (order_id,))
    count = cur.fetchone()[0]
    assert count == 2
