from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import ledger
from app.capital_manager import CapitalManager, reset_capital_manager
from app.main import create_app
from app.risk import accounting as risk_accounting
from app.risk import core as risk_core
from app.risk.daily_loss import get_daily_loss_cap_state
from app.services import runtime
from app.services.runtime import reset_for_tests
from app.strategy.pnl_tracker import reset_strategy_pnl_tracker_for_tests
from app.watchdog.exchange_watchdog import (
    get_exchange_watchdog,
    reset_exchange_watchdog_for_tests,
)
from pnl_history_store import reset_store as reset_pnl_history_store
from positions import close_position, create_position, reset_positions


TRADE_SYMBOL = "BTCUSDT"
TRADE_NOTIONAL = 5_000.0
TRADE_LEVERAGE = 2.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.mark.acceptance
def test_acceptance_trading_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN_MODE", "false")
    monkeypatch.setenv("RISK_CHECKS_ENABLED", "true")
    monkeypatch.setenv("ENFORCE_DAILY_LOSS_CAP", "1")
    monkeypatch.setenv("DAILY_LOSS_CAP_USDT", "5000")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_ENABLE", "false")
    monkeypatch.setenv("MODE", "paper")

    runtime_path = tmp_path / "runtime.json"
    positions_path = tmp_path / "positions.json"
    hedge_log_path = tmp_path / "hedge.json"
    ops_alerts_path = tmp_path / "ops_alerts.json"
    pnl_history_path = tmp_path / "pnl_history.json"
    approvals_path = tmp_path / "ops_approvals.json"
    daily_reports_path = tmp_path / "daily_reports.json"
    capital_state_path = tmp_path / "capital.json"

    for name, path in (
        ("RUNTIME_STATE_PATH", runtime_path),
        ("POSITIONS_STORE_PATH", positions_path),
        ("HEDGE_LOG_PATH", hedge_log_path),
        ("OPS_ALERTS_FILE", ops_alerts_path),
        ("PNL_HISTORY_PATH", pnl_history_path),
        ("OPS_APPROVALS_FILE", approvals_path),
        ("DAILY_REPORTS_PATH", daily_reports_path),
        ("CAPITAL_STATE_PATH", capital_state_path),
    ):
        monkeypatch.setenv(name, str(path))

    ledger_path = tmp_path / "ledger.db"
    monkeypatch.setattr(ledger, "LEDGER_PATH", ledger_path)

    reset_strategy_pnl_tracker_for_tests()
    risk_accounting.reset_risk_accounting_for_tests()
    risk_core.reset_risk_governor_for_tests()
    reset_for_tests()
    reset_positions()
    reset_pnl_history_store()
    reset_capital_manager(CapitalManager(state_path=capital_state_path))
    ledger.init_db()
    ledger.reset()
    monkeypatch.setattr(
        "services.opportunity_scanner.check_spread",
        lambda symbol: {
            "cheap": "binance",
            "expensive": "okx",
            "spread": 8.0,
            "spread_bps": 10.0,
        },
    )

    app = create_app()
    with TestClient(app) as client:
        reset_exchange_watchdog_for_tests()
        watchdog = get_exchange_watchdog()
        watchdog.check_once(
            lambda: {
                "binance": {"ok": True},
                "okx": {"ok": True},
            }
        )
        assert watchdog.overall_ok() is True

        risk_accounting.record_fill("paper", 0.0, 25.0, simulated=False)
        _, intent_result = risk_accounting.record_intent("paper", TRADE_NOTIONAL, simulated=False)
        assert intent_result["ok"] is True
        assert intent_result["state"] == "RECORDED"
        assert intent_result.get("reason") is None

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
        long_leg = position["legs"][0]
        short_leg = position["legs"][1]

        long_order = ledger.record_order(
            venue="binance-um",
            symbol=TRADE_SYMBOL,
            side="buy",
            qty=long_leg["base_size"],
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
            qty=long_leg["base_size"],
            price=entry_long_price,
            fee=0.0,
            ts=now_ts,
        )

        short_order = ledger.record_order(
            venue="okx-perp",
            symbol=TRADE_SYMBOL,
            side="sell",
            qty=short_leg["base_size"],
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
            qty=short_leg["base_size"],
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

        open_trades_csv = client.get("/api/ui/open-trades.csv")
        assert open_trades_csv.status_code == 200
        csv_rows = list(csv.reader(io.StringIO(open_trades_csv.text)))
        assert csv_rows
        header = csv_rows[0]
        assert header[:4] == ["trade_id", "pair", "side", "size"]

        close_all = client.post("/api/ui/trades/close-all")
        assert close_all.status_code == 200
        close_payload = close_all.json()
        assert isinstance(close_payload.get("closed"), list)

        exit_long_price = entry_long_price + 20.0
        exit_short_price = entry_short_price + 10.0
        close_position(
            position["id"],
            exit_long_price=exit_long_price,
            exit_short_price=exit_short_price,
        )
        risk_accounting.record_fill("paper", TRADE_NOTIONAL, 60.0, simulated=False)

        close_long_order = ledger.record_order(
            venue="binance-um",
            symbol=TRADE_SYMBOL,
            side="sell",
            qty=long_leg["base_size"],
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
            qty=long_leg["base_size"],
            price=exit_long_price,
            fee=0.0,
            ts=now_ts,
        )

        close_short_order = ledger.record_order(
            venue="okx-perp",
            symbol=TRADE_SYMBOL,
            side="buy",
            qty=short_leg["base_size"],
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
            qty=short_leg["base_size"],
            price=exit_short_price,
            fee=0.0,
            ts=now_ts,
        )

        reset_positions()

        risk_badges = client.get("/api/ui/runtime_badges")
        assert risk_badges.status_code == 200
        badges_payload = risk_badges.json()
        assert badges_payload["risk_checks"] in {"ON", "AUTO"}
        assert badges_payload["watchdog"] == "OK"
        assert badges_payload["daily_loss"] in {"OK", "WARN"}

        daily_loss_status = client.get("/api/ui/daily_loss_status")
        assert daily_loss_status.status_code == 200
        snapshot = daily_loss_status.json()
        assert snapshot["max_daily_loss_usdt"] == pytest.approx(5000.0)
        assert snapshot["breached"] is False
        assert snapshot["enabled"] is True

        state = runtime.get_state()
        assert state.control.environment == "paper"
        assert get_daily_loss_cap_state()["breached"] is False
