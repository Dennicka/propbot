from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.config.feature_flags as ff
from app.db import ledger
from app.router import smart_router as smart_router_module
from app.router.smart_router import SmartRouter


class DummyMarketData:
    def top_of_book(self, venue: str, symbol: str) -> dict[str, float]:
        return {"bid": 100.0, "ask": 101.0, "ts": time.time()}


def _make_router(monkeypatch: pytest.MonkeyPatch) -> SmartRouter:
    monkeypatch.setattr(smart_router_module, "get_liquidity_status", lambda: {})
    monkeypatch.setattr(smart_router_module, "get_profile", lambda: SimpleNamespace(name="test"))
    monkeypatch.setattr(smart_router_module, "metrics_write", lambda *args, **kwargs: None)
    monkeypatch.setattr(ff, "risk_limits_on", lambda: False)
    monkeypatch.setattr(ff, "md_watchdog_on", lambda: False)
    monkeypatch.setattr(ff, "pretrade_strict_on", lambda: False)
    state = SimpleNamespace(config=None)
    market_data = DummyMarketData()
    return SmartRouter(state=state, market_data=market_data)


def test_router_ledger_hooks(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db_path = tmp_path / "ledger.db"
    monkeypatch.setenv("LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("FF_LEDGER", "1")

    conn = ledger.init_db(str(db_path))

    router = _make_router(monkeypatch)

    base_order = {
        "order_id": "order-a",
        "intent_key": "intent-a",
        "strategy": "alpha",
        "symbol": "BTCUSDT",
        "venue": "binance",
        "side": "buy",
        "qty": Decimal("1"),
        "px": Decimal("100"),
    }
    router._ledger_on_begin(**base_order)
    router._ledger_on_ack(base_order["order_id"], "exch-a")
    router._ledger_on_final(base_order["order_id"], "REJECTED")

    statuses = ledger.fetch_orders_status()
    assert statuses[base_order["order_id"]] == "REJECTED"

    fill_order = {
        "order_id": "order-b",
        "intent_key": "intent-b",
        "strategy": "beta",
        "symbol": "ETHUSDT",
        "venue": "binance",
        "side": "sell",
        "qty": Decimal("0.5"),
        "px": Decimal("150"),
    }
    router._ledger_on_begin(**fill_order)
    router._ledger_on_ack(fill_order["order_id"], "exch-b")
    router._ledger_on_fill(
        order_id=fill_order["order_id"],
        ts=time.time(),
        qty=Decimal("0.25"),
        px=Decimal("150"),
        realized_pnl_usd=Decimal("1"),
    )
    router._ledger_on_fill(
        order_id=fill_order["order_id"],
        ts=time.time() + 1.0,
        qty=Decimal("0.25"),
        px=Decimal("151"),
        realized_pnl_usd=Decimal("2"),
    )
    router._ledger_on_final(fill_order["order_id"], "FILLED")

    cur = conn.execute("SELECT COUNT(*) FROM fills WHERE order_id=?", (fill_order["order_id"],))
    count = cur.fetchone()[0]
    assert count == 2

    statuses = ledger.fetch_orders_status()
    assert statuses[fill_order["order_id"]] == "FILLED"
