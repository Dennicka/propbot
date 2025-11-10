from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.pnl.ledger import PnLLedger, TradeFill
from app.strategy import reset_strategy_pnl_tracker_for_tests
from app.routers import ui as ui_router


def _make_basic_ledger() -> PnLLedger:
    ledger = PnLLedger()
    now = datetime.now(timezone.utc).timestamp()
    buy = TradeFill(
        venue="binance",
        symbol="BTCUSDT",
        side="BUY",
        qty=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0.1"),
        fee_asset="USDT",
        ts=now - 60.0,
    )
    sell = TradeFill(
        venue="binance",
        symbol="BTCUSDT",
        side="SELL",
        qty=Decimal("1"),
        price=Decimal("110"),
        fee=Decimal("0.1"),
        fee_asset="USDT",
        ts=now - 10.0,
    )
    ledger.apply_fill(buy, exclude_simulated=False)
    ledger.apply_fill(sell, exclude_simulated=False)
    return ledger


def test_strategy_pnl_endpoint_uses_ledger_snapshot(client, monkeypatch) -> None:
    reset_strategy_pnl_tracker_for_tests()
    ledger = _make_basic_ledger()

    def _fake_build(ctx, since, *, exclude_simulated):
        assert exclude_simulated is True
        return ledger

    monkeypatch.setattr(ui_router, "build_ledger_from_history", _fake_build)

    response = client.get("/api/ui/strategy_pnl")
    assert response.status_code == 200
    payload = response.json()

    rows = {row["name"]: row for row in payload["strategies"]}
    assert "BTCUSDT" in rows
    btc_row = rows["BTCUSDT"]
    assert pytest.approx(btc_row["realized_today"], rel=1e-6) == 9.8
    assert pytest.approx(btc_row["realized_7d"], rel=1e-6) == 9.8


def test_strategy_pnl_endpoint_simulated_toggle_from_ledger(client, monkeypatch) -> None:
    reset_strategy_pnl_tracker_for_tests()

    def _fake_build(ctx, since, *, exclude_simulated):
        ledger = PnLLedger()
        fill = TradeFill(
            venue="binance",
            symbol="SIM",
            side="BUY",
            qty=Decimal("1"),
            price=Decimal("1"),
            fee=Decimal("0"),
            fee_asset="USDT",
            ts=datetime.now(timezone.utc).timestamp(),
            is_simulated=True,
        )
        ledger.apply_fill(fill, exclude_simulated=exclude_simulated)
        return ledger

    monkeypatch.setattr(ui_router, "build_ledger_from_history", _fake_build)

    default_response = client.get("/api/ui/strategy_pnl")
    assert default_response.status_code == 200
    assert default_response.json()["strategies"] == []

    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", "false")
    include_response = client.get("/api/ui/strategy_pnl")
    assert include_response.status_code == 200
    payload = include_response.json()
    assert payload["simulated_excluded"] is False
    assert {row["name"] for row in payload["strategies"]} == {"SIM"}
