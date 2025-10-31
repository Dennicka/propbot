from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import ledger
from app.capital_manager import CapitalManager, reset_capital_manager
from app.main import create_app
from app.risk import accounting as risk_accounting
from app.services.runtime import reset_for_tests
from app.watchdog.exchange_watchdog import (
    get_exchange_watchdog,
    reset_exchange_watchdog_for_tests,
)
from app.strategy.pnl_tracker import reset_strategy_pnl_tracker_for_tests
from pnl_history_store import reset_store as reset_pnl_history_store
from positions import close_position, create_position, reset_positions


TRADE_SYMBOL = "BTCUSDT"
TRADE_NOTIONAL = 10_000.0
TRADE_LEVERAGE = 2.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def test_e2e_smoke_paper(monkeypatch, tmp_path) -> None:
    """Start the app, simulate a paper trade, close exposure, and validate operator APIs."""

    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "true")
    monkeypatch.setenv("DAILY_LOSS_CAP_DISABLED", "true")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_ENABLE", "false")
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("POSITIONS_STORE_PATH", str(tmp_path / "positions.json"))
    monkeypatch.setenv("HEDGE_LOG_PATH", str(tmp_path / "hedge.json"))
    monkeypatch.setenv("OPS_ALERTS_FILE", str(tmp_path / "ops_alerts.json"))
    monkeypatch.setenv("PNL_HISTORY_PATH", str(tmp_path / "pnl_history.json"))
    monkeypatch.setenv("OPS_APPROVALS_FILE", str(tmp_path / "ops_approvals.json"))
    monkeypatch.setenv("DAILY_REPORTS_PATH", str(tmp_path / "daily_reports.json"))
    monkeypatch.setenv("CAPITAL_STATE_PATH", str(tmp_path / "capital.json"))
    monkeypatch.setenv("STRATEGY_PNL_STATE_PATH", str(tmp_path / "strategy_pnl.json"))

    ledger_path = tmp_path / "ledger.db"
    monkeypatch.setattr(ledger, "LEDGER_PATH", ledger_path)

    reset_strategy_pnl_tracker_for_tests()
    risk_accounting.reset_risk_accounting_for_tests()
    reset_for_tests()
    reset_positions()
    reset_pnl_history_store()
    reset_capital_manager(CapitalManager(state_path=tmp_path / "capital.json"))
    ledger.init_db()
    ledger.reset()

    app = create_app()
    client = TestClient(app)

    reset_exchange_watchdog_for_tests()
    watchdog = get_exchange_watchdog()
    watchdog.check_once(lambda: {"binance": {"ok": True}, "okx": {"ok": True}})

    badges_resp = client.get("/api/ui/runtime_badges")
    assert badges_resp.status_code == 200
    badges = badges_resp.json()
    assert badges == {
        "auto_trade": "OFF",
        "risk_checks": "ON",
        "daily_loss": "OK",
        "watchdog": "OK",
    }

    risk_accounting.record_fill("paper", 0.0, 50.0, simulated=False)
    _, intent_result = risk_accounting.record_intent("paper", TRADE_NOTIONAL, simulated=False)
    assert intent_result["ok"] is True

    entry_long_price = 25_000.0
    entry_short_price = 25_010.0
    position = create_position(
        symbol=TRADE_SYMBOL,
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=TRADE_NOTIONAL,
        entry_spread_bps=12.5,
        leverage=TRADE_LEVERAGE,
        entry_long_price=entry_long_price,
        entry_short_price=entry_short_price,
        strategy="paper",
    )

    now_ts = _now_iso()
    long_order = ledger.record_order(
        venue="binance-um",
        symbol=TRADE_SYMBOL,
        side="buy",
        qty=position["legs"][0]["base_size"],
        price=entry_long_price,
        status="FILLED",
        client_ts=now_ts,
        exchange_ts=now_ts,
        idemp_key="paper-long-entry",
    )
    ledger.record_fill(
        order_id=long_order,
        venue="binance-um",
        symbol=TRADE_SYMBOL,
        side="buy",
        qty=position["legs"][0]["base_size"],
        price=entry_long_price,
        fee=0.0,
        ts=now_ts,
    )
    short_order = ledger.record_order(
        venue="okx-perp",
        symbol=TRADE_SYMBOL,
        side="sell",
        qty=position["legs"][1]["base_size"],
        price=entry_short_price,
        status="FILLED",
        client_ts=now_ts,
        exchange_ts=now_ts,
        idemp_key="paper-short-entry",
    )
    ledger.record_fill(
        order_id=short_order,
        venue="okx-perp",
        symbol=TRADE_SYMBOL,
        side="sell",
        qty=position["legs"][1]["base_size"],
        price=entry_short_price,
        fee=0.0,
        ts=now_ts,
    )

    pnl_resp = client.get("/api/ui/strategy_pnl")
    assert pnl_resp.status_code == 200
    pnl_payload = pnl_resp.json()
    strategies = {entry["name"]: entry for entry in pnl_payload["strategies"]}
    assert "paper" in strategies
    assert strategies["paper"]["realized_today"] >= 0.0
    assert pnl_payload["simulated_excluded"] is True

    open_trades_csv = client.get("/api/ui/open-trades.csv")
    assert open_trades_csv.status_code == 200
    assert open_trades_csv.headers["content-type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(open_trades_csv.text)))
    assert rows
    header = rows[0]
    assert header == [
        "trade_id",
        "pair",
        "side",
        "size",
        "entry_price",
        "unrealized_pnl",
        "opened_ts",
    ]
    data_rows = rows[1:]
    assert any(row[1] == TRADE_SYMBOL for row in data_rows)

    first_close = client.post("/api/ui/trades/close-all")
    assert first_close.status_code == 200
    first_payload = first_close.json()
    assert isinstance(first_payload.get("closed"), list)
    assert len(first_payload["closed"]) >= 1

    exit_long_price = entry_long_price + 20.0
    exit_short_price = entry_short_price + 10.0
    close_position(
        position["id"],
        exit_long_price=exit_long_price,
        exit_short_price=exit_short_price,
    )
    risk_accounting.record_fill("paper", TRADE_NOTIONAL, 0.0, simulated=False)

    close_long_order = ledger.record_order(
        venue="binance-um",
        symbol=TRADE_SYMBOL,
        side="sell",
        qty=position["legs"][0]["base_size"],
        price=exit_long_price,
        status="FILLED",
        client_ts=now_ts,
        exchange_ts=now_ts,
        idemp_key="paper-long-exit",
    )
    ledger.record_fill(
        order_id=close_long_order,
        venue="binance-um",
        symbol=TRADE_SYMBOL,
        side="sell",
        qty=position["legs"][0]["base_size"],
        price=exit_long_price,
        fee=0.0,
        ts=now_ts,
    )
    close_short_order = ledger.record_order(
        venue="okx-perp",
        symbol=TRADE_SYMBOL,
        side="buy",
        qty=position["legs"][1]["base_size"],
        price=exit_short_price,
        status="FILLED",
        client_ts=now_ts,
        exchange_ts=now_ts,
        idemp_key="paper-short-exit",
    )
    ledger.record_fill(
        order_id=close_short_order,
        venue="okx-perp",
        symbol=TRADE_SYMBOL,
        side="buy",
        qty=position["legs"][1]["base_size"],
        price=exit_short_price,
        fee=0.0,
        ts=now_ts,
    )
    reset_positions()

    second_close = client.post("/api/ui/trades/close-all")
    assert second_close.status_code == 200
    assert second_close.json() == {"closed": [], "positions": []}

    ops_report = client.get("/api/ui/ops_report")
    assert ops_report.status_code == 200
    report_payload = ops_report.json()
    assert isinstance(report_payload.get("badges"), dict)
    assert isinstance(report_payload.get("budgets"), list)
    assert isinstance(report_payload.get("strategy_pnl"), dict)
    assert report_payload.get("open_trades_count") == 0

    readiness = client.get("/live-readiness")
    assert readiness.status_code == 200
    assert readiness.json().get("ok") is True
